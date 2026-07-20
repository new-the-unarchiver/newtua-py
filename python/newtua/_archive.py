"""The archive itself: a lazily-opened sequence of entries."""

from __future__ import annotations

import errno
import os
import shutil
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
    """A spilled archive file that outlives whoever still needs it.

    Deleting on `Archive.close()` alone is not enough. For part of the formats
    (7z, RAR) the engine re-opens the archive **by path** at the moment an
    entry is read — see `sevenz.rs`, `read_entry`. With `stream=True` that read
    happens on a worker thread, after `open_stream` has already returned: the
    handshake only proves the file was there when the archive was opened, not
    when the entry is read. Closing the archive in between left the worker with
    no file and cut the pipe mid-entry.

    So the file is counted, not owned: the archive holds one claim, every live
    stream holds one more, and the last one released removes it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._holders = 1  # заводит его сам архив
        self._lock = threading.Lock()
        # Держится за путь, не за архив: иначе финализатор не дал бы архиву
        # собраться. Он же и удаляет — вызывается ровно один раз, кто бы ни
        # позвал: `release()` последнего держателя или сборщик мусора.
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
    """An archive opened for listing and extraction.

    Construction is lazy: nothing is read until the first access, which is what
    lets a password be supplied after asking whether one is needed.

    ```python
    with Archive("photos.zip") as ar:
        for entry in ar:
            print(entry.path, entry.size)
    ```
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
        # Имя → позиция, построенное разом вместе со списком записей: поиск по
        # имени иначе перебирал бы весь архив на каждое обращение.
        self._by_name: dict[PurePosixPath, Entry] | None = None
        self._tempfile: _TempFile | None = None
        self._closed = False

    # ── ленивое открытие ────────────────────────────────────────────────

    def _materialise(self) -> Path:
        """Give the engine a real path, spilling bytes or streams to disk."""
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
                # Кусками, а не `source.read()`: тот держал бы весь архив в
                # памяти целиком ради одной лишь перекладки на диск.
                shutil.copyfileobj(source, fh)
            self._tempfile = _TempFile(Path(fh.name))
        return self._tempfile.path

    def _open(self) -> _newtua.Archive:
        if self._closed:
            raise ValueError("operation on a closed archive")
        if self._reader is None:
            path = self._materialise()
            # Проверяем существование сами: движок сообщает об отсутствии файла
            # как NewtuaError(kind="io"), который raise_for разбирает только до
            # OSError, — а протокол требует именно FileNotFoundError. Текст
            # ошибки при этом не разбираем, полагаемся только на pathlib.
            if self._has_own_path() and not path.exists():
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), str(path)
                )
            try:
                self._reader = _newtua.open(
                    str(path), password=self._password, encoding=self._encoding
                )
            except Exception as exc:  # исключение скомпилированного модуля
                if self._tempfile is not None:
                    self._tempfile.release()
                    self._tempfile = None
                raise_for(exc)
        return self._reader

    def _load(self) -> tuple[Entry, ...]:
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
            # Имена в архивах повторяются, а по имени должна находиться первая
            # такая запись — `setdefault` первую и оставляет.
            by_name: dict[PurePosixPath, Entry] = {}
            for entry in self._entries:
                by_name.setdefault(entry.path, entry)
            self._by_name = by_name
        return self._entries

    # ── протокол последовательности ─────────────────────────────────────

    def __len__(self) -> int:
        """Number of entries."""
        return len(self._load())

    def __iter__(self) -> Iterator[Entry]:
        """Iterate entries in archive order."""
        return iter(self._load())

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
        entries = self._load()
        if isinstance(key, slice):
            return entries[key]
        if isinstance(key, int):
            return entries[key]
        # `_load()` выше уже построил указатель по именам.
        assert self._by_name is not None
        entry = self._by_name.get(PurePosixPath(key))
        if entry is None:
            raise EntryNotFoundError(str(key))
        return entry

    def __contains__(self, key: object) -> bool:
        """Whether an entry with that name (or that entry) is present."""
        if isinstance(key, Entry):
            return key in self._load()
        if isinstance(key, (str, PurePosixPath)):
            try:
                self[key]
            except EntryNotFoundError:
                return False
            return True
        return False

    def __repr__(self) -> str:
        # Must never open anything: uses only state already loaded by an
        # earlier `_load()` call, never `self.format` (which can open the
        # archive) or any other lazy accessor.
        if self._entries is None:
            return f"<newtua Archive: {self._source!r}, not opened yet>"
        # `_entries` is only ever set right after `_open()` succeeds (see
        # `_load()`), so the reader is guaranteed to be there too.
        assert self._reader is not None
        total = sum(e.size for e in self._entries)
        return (
            f"<newtua Archive: {Format(self._reader.format())}, "
            f"{len(self._entries)} entries, {_human_size(total)}>"
        )

    # ── свойства ────────────────────────────────────────────────────────

    @property
    def format(self) -> Format:
        """Container format of this archive."""
        return Format(self._open().format())

    @property
    def needs_password(self) -> bool:
        """Whether a password is required to read this archive's contents."""
        try:
            return any(e.is_encrypted for e in self._load())
        except (PasswordRequiredError, WrongPasswordError):
            # Заголовки зашифрованы — даже перечислить нельзя без пароля.
            # Перехват именно этих двух, а не всех ошибок движка: на файле,
            # который вовсе не архив, «нужен пароль» было бы враньём, да ещё и
            # проглотившим настоящую причину (формат не распознан).
            return True

    @property
    def password(self) -> str | None:
        """The password in use, if any."""
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        self._password = value
        # Пароль задаётся при открытии, поэтому читатель надо завести заново.
        self._reader = None
        self._entries = None
        self._by_name = None

    @property
    def detected_encoding(self) -> str:
        """Charset the engine picked for the entry names.

        Decided by the engine at open time, over every entry's real bytes at
        once — not recomputed here, because one common verdict for the whole
        archive is what the engine's own decoding is based on.
        """
        return self._open().detected_encoding()

    # ── чтение ──────────────────────────────────────────────────────────

    def _has_own_path(self) -> bool:
        """Whether the source is a real path, not bytes or a stream."""
        return isinstance(self._source, (str, os.PathLike))

    def _wrapper_name_source(self) -> str | None:
        """Path the wrapper folder should be named after, or None if there is none.

        Только здесь и решается: движок ничего не подставляет от себя. Иначе он
        взял бы собственный путь архива, а это случайный `tmpXXXXXXXX` всякий
        раз, когда источником были байты или поток, — деталь реализации,
        вылезшая в папку пользователя. Про то, откуда взялся архив, знает один
        этот класс.
        """
        source = self._source
        if isinstance(source, (str, os.PathLike)):
            return os.fspath(source)
        name = getattr(source, "name", None)
        # `name` у файлового объекта — не всегда путь: у сокетов и у потоков,
        # открытых по дескриптору, это целое число, а у BytesIO его нет вовсе.
        if isinstance(name, str) and name:
            return name
        return None  # имени нет вовсе — значит и обёртки не будет

    def _index_of(self, ref: int | str | PurePosixPath | Entry) -> int:
        """Position of an entry, given a position, a name, or the entry itself."""
        entries = self._load()
        if isinstance(ref, int):
            if not -len(entries) <= ref < len(entries):
                raise IndexError(ref)
            return ref % len(entries)
        # Позицию берём из самой записи: искать её сравнением нельзя — имена в
        # архивах повторяются, и нашлась бы первая попавшаяся.
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
        """Open one entry as a file-like object.

        With `stream=False` (the default) the entry is decoded into spillable
        storage first, which keeps the result seekable.

        With `stream=True` the entry is decoded on a worker thread into an OS
        pipe and read from the other end: memory stays flat however large the
        entry is, and the first bytes arrive at once — but the result does not
        rewind (`seekable()` is `False`).
        """
        reader = self._open()
        index = self._index_of(entry)
        if stream:
            size = self._load()[index].size
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
        if os.name != "posix":
            raise NotImplementedError("stream=True needs POSIX pipes")
        read_fd, write_fd = os.pipe()
        # Заявка на временный файл берётся до вызова и живёт, пока жив стрим:
        # часть форматов переоткрывает архив по пути прямо в `read_entry`, уже
        # на рабочем потоке. Рукопожатие ниже до этой точки не достаёт.
        tempfile = self._tempfile
        if tempfile is not None:
            tempfile.hold()
        try:
            # Вызов не возвращается, пока рабочий поток не открыл архив, — так
            # что ошибка открытия придёт сюда исключением, а не пустым каналом.
            reader.open_stream(index, write_fd)
        except BaseException as exc:
            # Любая ошибка оставляет write_fd за нами: Rust берёт его во
            # владение только на удачном пути. Закрываем оба конца один раз,
            # а дальше решаем, во что превратить саму ошибку.
            os.close(read_fd)
            os.close(write_fd)
            if tempfile is not None:
                tempfile.release()
            if isinstance(exc, Exception):
                raise_for(exc)  # исключение скомпилированного модуля
            raise  # KeyboardInterrupt и подобные — не наши
        # Дальше write_fd принадлежит Rust: рабочий поток закроет его сам, и
        # это закрытие и есть признак конца данных для читающего конца.
        #
        # `os.fdopen`, а не встроенный `open`: имя `open` здесь — метод этого
        # же класса, и читать такое неприятно. Буферизация оставлена включённой
        # (по умолчанию): без неё `read(n)` сводился бы к одному системному
        # вызову и возвращал бы сколько пришло, а `io.BufferedIOBase` обещает
        # ровно `n` байт, пока данные не кончились.
        return _PipeStream(
            os.fdopen(read_fd, "rb"),
            expected_size=expected_size,
            on_close=None if tempfile is None else tempfile.release,
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
        # `name_source is None` движок читает как «обёртки не будет».
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

    # ── закрытие ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the archive and delete any temporary file it made."""
        self._reader = None
        self._entries = None
        self._by_name = None
        self._closed = True
        if self._tempfile is not None:
            # Только снимаем свою заявку: файл переживёт архив, если его ещё
            # читает хоть один стрим.
            self._tempfile.release()
            self._tempfile = None

    def __enter__(self) -> Archive:
        # `_load()` opens the reader too; loading entries here rather than
        # just opening means `repr()` reports real counts as soon as the
        # `with` block is entered, not only after some other access forces it.
        self._load()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
