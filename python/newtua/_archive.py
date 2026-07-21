"""The archive itself: a lazily-opened sequence of entries."""

import errno
import os
import shutil
import sys
import threading
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import IO, BinaryIO, Callable, Iterator, Sequence, overload

from newtua import _newtua
from newtua._entry import Entry, EntryKind
from newtua._errors import (
    EntryNotFoundError,
    PasswordRequiredError,
    WrongPasswordError,
    raise_for,
)
from newtua._events import ProgressEvent, event_from_raw
from newtua._format import Format
from newtua._stream import EntryStream, _PipeStream

__all__ = ["Archive", "Report"]


@dataclass(frozen=True)
class Report:
    """Outcome of an extraction."""

    extracted: int
    failed: int
    aborted: bool


def _delete_tempfile(path: Path) -> None:
    """Remove a spilled temp file. Takes only the path (never the archive)."""
    path.unlink(missing_ok=True)


class _TempFile:
    """
    A spilled archive file that outlives whoever still needs it.

    Deleting on `Archive.close()` alone is not enough. For part of the formats
    (7z, RAR) the engine re-opens the archive **by path** at the moment an
    entry is read — see `sevenz.rs`, `read_entry`. With `stream=True` that
    read happens on a worker thread, after `open_stream` has already returned:
    the handshake only proves the file was there when the archive was opened, not
    when the entry is read. Closing the archive in between left the worker with
    no file and cut the pipe mid-entry.

    So the file is counted, not owned: the archive holds one claim, every live
    stream holds one more, and the last one released removes it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._holders = 1  # The archive itself creates it.
        self._lock = threading.Lock()
        # Holds the path, not the archive: otherwise the finalizer would not
        # let the archive collect. It also deletes — called exactly once,
        # whoever released the last holder or the garbage collector.
        self._delete = weakref.finalize(self, _delete_tempfile, path)

    def hold(self) -> None:
        """Claim the file; the holder must `release()` it exactly once."""
        with self._lock:
            self._holders += 1

    def release(self) -> None:
        """Give up one claim, deleting the file when the last one goes."""
        with self._lock:
            self._holders -= 1
            if self._holders > 0:
                return
        self._delete()


def _safe_mtime(raw: float | None) -> datetime | None:
    """Convert a raw timestamp, tolerating garbage from untrusted archives."""
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _safe_kind(raw: str) -> EntryKind:
    """Convert a raw entry kind, falling back to FILE for anything unrecognised."""
    try:
        return EntryKind(raw)
    except ValueError:
        return EntryKind.FILE


def _human_size(n: int) -> str:
    """Render a byte count the way a person would read it."""
    units = ("B", "KB", "MB", "GB", "TB")
    step = 1024.0
    size = float(n)
    index = 0
    while size >= step and index < len(units) - 1:
        size /= step
        index += 1
    unit = units[index]
    return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"


class Archive:
    """
    An archive opened for listing and extraction.

    Construction is lazy: nothing is read until the first access, which is what
    lets a password be supplied after asking whether one is needed.

    ```python
    with Archive("photos.zip") as ar:
        print(ar.format)
        for entry in ar:
            print(entry.path, entry.size)
        ar.extract("destination/folder")
    ```

    ### Properties:
    - `format`: the container format of the archive
    - `needs_password`: whether a password is required to read the archive's contents
    - `password`: the password in use, if any
    - `detected_encoding`: the charset the engine picked for the entry names

    ### Methods:
    - `read`: read one entry entirely into memory
    - `open`: open one entry as a file-like object
    - `extract`: extract entries into a destination directory
    - `close`: close the archive and delete any temporary file it made

    ### Context manager:
    ```python
    with Archive("photos.zip") as ar:
        ar.extract("destination/folder")
    ```

    ### Sequence protocol:
    ```python
    len(ar)
    for entry in ar: ...
    ar[0]
    ar["photo.jpg"]
    ar[0:10]
    "photo.jpg" in ar
    ```

    ### String representation:
    ```python
    repr(ar)
    ```

    ### Exceptions:
    - `EntryNotFoundError`: if an entry with that name (or that entry) is not found
    - `PasswordRequiredError`: if a password is required to read the archive's contents
    - `WrongPasswordError`: if the password is incorrect
    - `NotImplementedError`: if a feature is not implemented
    - `ValueError`: if an operation is performed on a closed archive
    - `OSError`: if an I/O error occurs
    """

    def __init__(
        self,
        source: str | os.PathLike[str] | bytes | BinaryIO,
        *,
        password: str | None = None,
        encoding: str | None = None,
    ) -> None:
        self._source = source
        self._password = password
        self._encoding = encoding
        # Explicit union, not the inferred `None`: `__repr__` reads this
        # attribute directly (not through `_open()`) once entries are loaded.
        self._reader: _newtua.Archive | None = None
        self._entries: tuple[Entry, ...] | None = None
        # Name → first entry, built with the list: otherwise a name lookup
        # would scan the whole archive on each access.
        self._by_name: dict[PurePosixPath, Entry] | None = None
        self._tempfile: _TempFile | None = None
        self._closed = False

    # ── lazy opening ─────────────────────────────────────────────────────

    def _backing_path(self) -> Path:
        """Path to the real on-disk file behind the archive, spilling bytes or
        streams to a temp file when the source has none of its own."""
        source = self._source
        if isinstance(source, (str, os.PathLike)):
            return Path(source)
        # A password retry (via the `password` setter) resets the reader and
        # entries so the archive reopens, but the source's bytes/stream don't
        # change with the password — reuse the spilled file already on disk
        # instead of spilling a fresh one and orphaning the old one.
        if self._tempfile is not None and self._tempfile.path.exists():
            return self._tempfile.path
        with NamedTemporaryFile(suffix=".newtua", delete=False) as fh:
            if isinstance(source, bytes):
                fh.write(source)
            else:
                # In chunks, not `source.read()`: that would keep the whole
                # archive in memory just to transfer it to disk.
                shutil.copyfileobj(source, fh)
            self._tempfile = _TempFile(Path(fh.name))
        return self._tempfile.path

    def _open(self) -> _newtua.Archive:
        """Open the archive as an I/O object."""
        if self._closed:
            raise ValueError("operation on a closed archive")
        if self._reader is None:
            path = self._backing_path()
            # Check existence ourselves: the engine reports missing files as
            # NewtuaError(kind="io"), which raise_for only turns into OSError,
            # and the protocol requires FileNotFoundError. The error text is
            # not parsed, relying only on pathlib.
            if self._has_own_path() and not path.exists():
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), str(path)
                )
            try:
                self._reader = _newtua.open(
                    str(path), password=self._password, encoding=self._encoding
                )
            except Exception as exc:  # compiled module exception
                if self._tempfile is not None:
                    self._tempfile.release()
                    self._tempfile = None
                raise_for(exc)
        return self._reader

    def _listing(self) -> tuple[Entry, ...]:
        """The archive's table of contents: entry metadata only, never entry
        bytes. Built once from the engine's listing snapshot, then cached."""
        if self._entries is None:
            reader = self._open()
            raw = reader.entries()
            self._entries = tuple(
                Entry(
                    index=i,
                    path=PurePosixPath(r.path),
                    raw_name=r.raw_name,
                    kind=_safe_kind(r.kind),
                    size=r.size,
                    is_encrypted=r.is_encrypted,
                    mode=r.mode,
                    mtime=_safe_mtime(r.mtime),
                    _archive=self,
                )
                for i, r in enumerate(raw)
            )
            # Names in archives repeat, and the first such entry must
            # be found by name — `setdefault` keeps the first one.
            by_name: dict[PurePosixPath, Entry] = {}
            for entry in self._entries:
                by_name.setdefault(entry.path, entry)
            self._by_name = by_name
        return self._entries

    # ── sequence protocol ────────────────────────────────────────────────

    def __len__(self) -> int:
        """Number of entries."""
        return len(self._listing())

    def __iter__(self) -> Iterator[Entry]:
        """Iterate entries in archive order."""
        return iter(self._listing())

    @overload
    def __getitem__(self, key: int) -> Entry: ...
    @overload
    def __getitem__(self, key: str | PurePosixPath) -> Entry: ...
    @overload
    def __getitem__(self, key: slice) -> tuple[Entry, ...]: ...

    def __getitem__(
        self, key: int | str | PurePosixPath | slice
    ) -> Entry | tuple[Entry, ...]:
        """Entry by position, by name, or a tuple of them by slice."""
        entries = self._listing()
        if isinstance(key, slice):
            return entries[key]
        if isinstance(key, int):
            return entries[key]
        # `_listing()` already built the name index.
        assert self._by_name is not None
        entry = self._by_name.get(PurePosixPath(key))
        if entry is None:
            raise EntryNotFoundError(str(key))
        return entry

    def __contains__(self, key: object) -> bool:
        """Whether an entry with that name (or that entry) is present."""
        if isinstance(key, Entry):
            return key in self._listing()
        if isinstance(key, (str, PurePosixPath)):
            self._listing()
            assert self._by_name is not None
            return PurePosixPath(key) in self._by_name
        return False

    def __repr__(self) -> str:
        # Must never open anything: uses only state already loaded by an
        # earlier `_listing()` call, never `self.format` (which can open the
        # archive) or any other lazy accessor.
        if self._entries is None:
            return f"<newtua Archive: {self._source!r}, not opened yet>"
        # `_entries` is only ever set right after `_open()` succeeds (see
        # `_listing()`), so the reader is guaranteed to be there too.
        assert self._reader is not None
        total = sum(e.size for e in self._entries)
        return (
            f"<newtua Archive: {Format(self._reader.format())}, "
            f"{len(self._entries)} entries, {_human_size(total)}>"
        )

    # ── properties ───────────────────────────────────────────────────────

    @property
    def format(self) -> Format:
        """Container format of this archive."""
        return Format(self._open().format())

    @property
    def needs_password(self) -> bool:
        """Whether a password is required to read this archive's contents."""
        try:
            return any(e.is_encrypted for e in self._listing())
        except (PasswordRequiredError, WrongPasswordError):
            # Headers are encrypted — even listing them is impossible without a password.
            # Catch only these two, not all engine errors: on a file that is
            # not an archive, «needs password» would be a lie, and it would
            # swallow the real reason (format not recognised).
            return True

    @property
    def password(self) -> str | None:
        """The password in use, if any."""
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        """Set the password in use, if any."""
        self._password = value
        # The password is set at open, so the reader must be re-created.
        self._reader = None
        self._entries = None
        self._by_name = None

    @property
    def detected_encoding(self) -> str:
        """
        Charset the engine picked for the entry names.

        Decided by the engine at open time, over every entry's real bytes at
        once — not recomputed here, because one common verdict for the whole
        archive is what the engine's own decoding is based on.
        """
        return self._open().detected_encoding()

    # ── reading ──────────────────────────────────────────────────────────

    def _has_own_path(self) -> bool:
        """Whether the source is a real path, not bytes or a stream."""
        return isinstance(self._source, (str, os.PathLike))

    def _wrapper_name_source(self) -> str | None:
        """
        Path the wrapper folder should be named after, or `None` if there is none.

        Decided here only: the engine must not fall back to its own path, or a
        spilled bytes/stream source would surface as a random `tmpXXXXXXXX`
        folder. Only this class knows where the archive came from.
        """
        source = self._source
        if isinstance(source, (str, os.PathLike)):
            return os.fspath(source)
        name = getattr(source, "name", None)
        # `name` on a file object is not always a path: sockets and fd-backed
        # streams use an int, and BytesIO has no name at all.
        if isinstance(name, str) and name:
            return name
        return None

    def _index_of(self, ref: int | str | PurePosixPath | Entry) -> int:
        """Position of an entry, given a position, a name, or the entry itself."""
        entries = self._listing()
        if isinstance(ref, int):
            if not -len(entries) <= ref < len(entries):
                raise IndexError(ref)
            return ref % len(entries)
        # Take the position from the entry itself: searching by comparison
        # is not possible — names repeat in archives, and the first one
        # found would be returned.
        entry = ref if isinstance(ref, Entry) else self[ref]
        return entry.index

    def read(self, entry: int | str | PurePosixPath | Entry) -> bytes:
        """Read one entry entirely into memory."""
        reader = self._open()
        index = self._index_of(entry)
        try:
            return reader.read(index)
        except Exception as exc:
            raise_for(exc)

    def open(
        self, entry: int | str | PurePosixPath | Entry, *, stream: bool = False
    ) -> EntryStream:
        """
        Open one entry as a file-like object.

        With `stream=False` (the default) the entry is decoded into spillable
        storage first, which keeps the result seekable.

        With `stream=True` the entry is decoded on a worker thread into an OS
        pipe and read from the other end: memory stays flat however large the
        entry is, and the first bytes arrive at once — but the result does not
        rewind (`seekable()` is `False`).

        On Windows, `stream=True` requires a file-path source: a bytes or stream
        source raises `NotImplementedError` there (use `stream=False` or a path).
        """
        reader = self._open()
        index = self._index_of(entry)
        if stream:
            size = self._listing()[index].size
            return self._open_pipe(reader, index, expected_size=size)

        def write_into(sink: IO[bytes]) -> None:
            try:
                reader.write_entry_to(index, sink)
            except Exception as exc:
                raise_for(exc)

        return EntryStream.from_writer(write_into)

    def _open_pipe(
        self, reader: _newtua.Archive, index: int, *, expected_size: int | None = None
    ) -> EntryStream:
        """Decode one entry through an OS pipe fed by a worker thread."""
        if os.name == "posix":
            return self._open_pipe_posix(reader, index, expected_size=expected_size)
        if os.name == "nt":
            return self._open_pipe_windows(reader, index, expected_size=expected_size)
        raise NotImplementedError("stream=True needs POSIX pipes or Windows")

    def _open_pipe_posix(
        self, reader: _newtua.Archive, index: int, *, expected_size: int | None = None
    ) -> EntryStream:
        """POSIX path: Python creates the pipe and hands the write end to Rust."""
        read_fd, write_fd = os.pipe()
        # Claim the tempfile before the call and keep it for the stream's
        # lifetime: some formats reopen the archive by path in `read_entry` on
        # the worker thread, past the handshake below.
        tempfile = self._tempfile
        if tempfile is not None:
            tempfile.hold()
        try:
            # Does not return until the worker has opened the archive, so an
            # open error arrives here as an exception, not an empty pipe.
            reader.open_stream(index, write_fd)
        except BaseException as exc:
            # Any error leaves write_fd with us: Rust takes ownership only on
            # success. Close both ends once, then map or re-raise the error.
            os.close(read_fd)
            os.close(write_fd)
            if tempfile is not None:
                tempfile.release()
            if isinstance(exc, Exception):
                raise_for(exc)  # compiled module exception
            raise  # KeyboardInterrupt and similar — not ours
        # write_fd now belongs to Rust; the worker closes it, which signals EOF
        # on the read end.
        #
        # `os.fdopen`, not the built-in `open`: `open` here is this class's
        # method. Buffering stays on (default): without it `read(n)` is one
        # syscall returning whatever arrived, while `io.BufferedIOBase`
        # promises exactly `n` bytes until EOF.
        return _PipeStream(
            os.fdopen(read_fd, "rb"),
            expected_size=expected_size,
            on_close=None if tempfile is None else tempfile.release,
        )

    def _open_pipe_windows(
        self, reader: _newtua.Archive, index: int, *, expected_size: int | None = None
    ) -> EntryStream:
        """Windows path: Rust creates the pipe; Python adopts the read handle.

        Only path sources are supported. A bytes or stream source is spilled to
        a temp file, and on Windows the worker holds that file open while the
        archive is read (7z/RAR reopen by path), so it cannot be unlinked —
        rejected up front rather than failing later on cleanup.
        """
        if not self._has_own_path():
            raise NotImplementedError(
                "stream=True is not supported for in-memory (bytes) or stream "
                "sources on Windows; use a file path, or stream=False"
            )
        # This early return narrows `sys.platform` for type checkers, so the
        # Windows-only `msvcrt` import below is not flagged on other platforms.
        # At runtime the method is only reached when `os.name == "nt"`.
        if sys.platform != "win32":  # pragma: no cover - dispatched only on Windows
            raise NotImplementedError("the Windows streaming path needs a Windows build")
        import msvcrt

        try:
            # Rust owns both pipe ends until this returns; on an open error there
            # is nothing on our side to close. It returns the read HANDLE.
            handle = reader.open_stream_windows(index)
        except Exception as exc:  # compiled module exception
            raise_for(exc)
        # open_osfhandle transfers ownership of the HANDLE to the fd: closing the
        # fd closes the HANDLE. No _TempFile is involved (path source only).
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        return _PipeStream(
            os.fdopen(fd, "rb"), expected_size=expected_size, on_close=None
        )

    def extract(
        self,
        dest: str | os.PathLike[str],
        *,
        selection: Sequence[int | str | PurePosixPath | Entry] | None = None,
        wrapper: bool = True,
        strict: bool = False,
        preserve: bool = True,
        progress: Callable[[ProgressEvent], bool | None] | None = None,
    ) -> Report:
        """Extract entries into `dest`.

        `progress` receives one event object per step; returning `False`
        cancels the extraction.
        """
        reader = self._open()
        indices = (
            [self._index_of(ref) for ref in selection]
            if selection is not None
            else None
        )
        # The engine treats `name_source is None` as «no wrapper».
        name_source = self._wrapper_name_source()

        raw_progress: Callable[[str, int, str | None, int, int], bool | None] | None
        raw_progress = None
        if progress is not None:

            def raw_progress(
                event: str, index: int, path: str | None, written: int, size: int
            ) -> bool | None:
                return progress(event_from_raw(event, index, path, written, size))

        try:
            report = reader.extract(
                str(dest),
                selection=indices,
                wrapper=wrapper,
                strict=strict,
                preserve=preserve,
                progress=raw_progress,
                name_source=name_source,
            )
        except Exception as exc:
            raise_for(exc)
        return Report(
            extracted=report.extracted, failed=report.failed, aborted=report.aborted
        )

    # ── closing ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the archive and delete any temporary file it made."""
        self._reader = None
        self._entries = None
        self._by_name = None
        self._closed = True
        if self._tempfile is not None:
            # Only release our claim: the file will survive the archive if
            # it is still read by at least one stream.
            self._tempfile.release()
            self._tempfile = None

    def __enter__(self) -> 'Archive':
        # `_listing()` opens the reader too; loading entries here rather than
        # just opening means `repr()` reports real counts as soon as the
        # `with` block is entered, not only after some other access forces it.
        self._listing()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
