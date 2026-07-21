"""Asyncio-facing archive: the sync engine, off the event loop.

Every byte-touching call is a stateless re-open from the backing path with the
GIL released (see `_newtua.*_path`), so nothing freezes the loop and no
`unsendable` reader is pinned to a thread. Entry metadata is cached at open and
served synchronously — iterating entries is pure Python, never I/O.
"""

import asyncio
import errno
import os
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import BinaryIO, Callable, Iterator, Sequence, overload

from newtua import _newtua
from newtua._archive import (
    Archive,
    Report,
    _entries_from_raw,
    _resolve_index,
    _wrap_progress,
)
from newtua._entry import Entry
from newtua._errors import EntryNotFoundError, raise_for
from newtua._events import ProgressEvent
from newtua._format import Format

__all__ = ["AsyncArchive"]


class AsyncArchive:
    """An archive opened for async listing and extraction.

    ```python
    async with newtua.AsyncArchive("big.dmg") as ar:
        for entry in ar:                     # sync: metadata only
            print(entry.path, entry.size)
        data = await ar.read("readme.txt")   # off the loop
        report = await ar.extract("out/")    # off the loop
    ```

    Entries are metadata-only: use `await ar.read(entry)` / `await ar.extract(...)`,
    not `entry.read()`. The progress callback runs on a worker thread, not the
    event loop — to touch the loop from it, use `loop.call_soon_threadsafe`.
    """

    def __init__(
        self,
        source: str | os.PathLike[str] | bytes | BinaryIO,
        *,
        password: str | None = None,
        encoding: str | None = None,
    ) -> None:
        # A sync Archive is reused ONLY for source plumbing (spill to temp,
        # tempfile lifetime, wrapper-name source). Its reader is never opened.
        self._sync = Archive(source, password=password, encoding=encoding)
        self._password = password
        self._encoding = encoding
        self._path: Path | None = None
        self._entries: tuple[Entry, ...] = ()
        self._by_name: dict[PurePosixPath, Entry] = {}
        self._format: str | None = None
        self._detected_encoding: str | None = None

    async def __aenter__(self) -> "AsyncArchive":
        # Spilling bytes/stream to a temp file and reading the listing are both
        # blocking — do them off the loop.
        self._path = await asyncio.to_thread(self._sync._backing_path)
        # Check existence ourselves, same reasoning as `Archive._open()`: the
        # engine reports missing files as NewtuaError(kind="io"), which
        # raise_for only turns into OSError, and the protocol requires
        # FileNotFoundError.
        if self._sync._has_own_path() and not self._path.exists():
            self._sync.close()  # releases the tempfile claim, if any
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), str(self._path)
            )
        try:
            raw, fmt, enc = await asyncio.to_thread(
                _newtua.list_path, str(self._path), self._password, self._encoding
            )
        except Exception as exc:  # compiled module exception
            # __aexit__ never runs on a failed __aenter__, so release the
            # spilled temp file (if any) ourselves before propagating.
            self._sync.close()
            raise_for(exc)
        # owner=None: metadata-only entries (no reachable blocking entry.read()).
        self._entries, self._by_name = _entries_from_raw(raw, None)
        self._format = fmt
        self._detected_encoding = enc
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._sync.close()  # releases the tempfile claim, if any

    # ── sync sequence over cached metadata ───────────────────────────────
    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries)

    @overload
    def __getitem__(self, key: int) -> Entry: ...
    @overload
    def __getitem__(self, key: str | PurePosixPath) -> Entry: ...
    @overload
    def __getitem__(self, key: slice) -> tuple[Entry, ...]: ...

    def __getitem__(
        self, key: int | str | PurePosixPath | slice
    ) -> Entry | tuple[Entry, ...]:
        if isinstance(key, slice):
            return self._entries[key]
        if isinstance(key, int):
            return self._entries[key]
        entry = self._by_name.get(PurePosixPath(key))
        if entry is None:
            raise EntryNotFoundError(str(key))
        return entry

    def __contains__(self, key: object) -> bool:
        if isinstance(key, Entry):
            return key in self._entries
        if isinstance(key, (str, PurePosixPath)):
            return PurePosixPath(key) in self._by_name
        return False

    # ── properties ───────────────────────────────────────────────────────
    @property
    def format(self) -> Format:
        assert self._format is not None
        return Format(self._format)

    @property
    def detected_encoding(self) -> str:
        assert self._detected_encoding is not None
        return self._detected_encoding

    @property
    def needs_password(self) -> bool:
        return any(e.is_encrypted for e in self._entries)

    # ── async reading / extraction ───────────────────────────────────────
    async def read(self, entry: int | str | PurePosixPath | Entry) -> bytes:
        """Read one entry entirely into memory, off the loop."""
        index = _resolve_index(self._entries, entry)
        try:
            return await asyncio.to_thread(
                _newtua.read_path, str(self._path), index, self._password, self._encoding
            )
        except Exception as exc:
            raise_for(exc)

    async def extract(
        self,
        dest: str | os.PathLike[str],
        *,
        selection: Sequence[int | str | PurePosixPath | Entry] | None = None,
        wrapper: bool = True,
        strict: bool = False,
        preserve: bool = True,
        progress: Callable[[ProgressEvent], bool | None] | None = None,
    ) -> Report:
        """Extract entries into `dest`, off the loop."""
        indices = (
            [_resolve_index(self._entries, ref) for ref in selection]
            if selection is not None
            else None
        )
        name_source = self._sync._wrapper_name_source()
        raw_progress = _wrap_progress(progress) if progress is not None else None
        try:
            r = await asyncio.to_thread(
                _newtua.extract_path,
                str(self._path),
                str(dest),
                indices,
                wrapper,
                strict,
                preserve,
                raw_progress,
                name_source,
                self._password,
                self._encoding,
            )
        except Exception as exc:
            raise_for(exc)
        return Report(extracted=r.extracted, failed=r.failed, aborted=r.aborted)
