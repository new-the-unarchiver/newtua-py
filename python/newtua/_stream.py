"""File-like access to a single archive entry."""

# Same reason as in `_entry.py`: `WriteableBuffer` exists for the type checker
# only, so the annotations naming it must not be evaluated at import time.
from __future__ import annotations

import io
from tempfile import SpooledTemporaryFile
from typing import IO, TYPE_CHECKING, Callable, Protocol

from newtua._errors import CorruptArchiveError

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

__all__ = ["EntryStream"]

#: Entries below this stay in memory; larger ones spill to a temporary file.
MAX_IN_MEMORY = 8 * 1024 * 1024


class _Backing(Protocol):
    """
    The reading surface a backing store must offer.
    """

    # Narrower than `IO[bytes]` on purpose: `readinto` is the part that
    # matters here (it is what lets a caller's buffer be filled without
    # an intermediate here (it is what lets a caller's buffer be filled
    # without an intermediate copy), and `IO[bytes]` does not promise it.
    # Both backings in use — a `SpooledTemporaryFile` and a pipe opened
    # by `os.fdopen` — do.

    def read(self, size: int = -1, /) -> bytes: ...
    def readinto(self, buffer: WriteableBuffer, /) -> int: ...
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def tell(self) -> int: ...
    def close(self) -> None: ...


class EntryStream(io.BufferedIOBase):
    """
    A binary file-like object over one entry's contents.
    """

    # Its own class rather than a bare `SpooledTemporaryFile`: the backing
    # is an implementation detail that may change, and callers should not
    # depend on it.

    def __init__(self, backing: _Backing) -> None:
        self._backing = backing

    @classmethod
    def from_writer(
        cls,
        write_into: Callable[[IO[bytes]], None],
        *,
        max_in_memory: int = MAX_IN_MEMORY,
    ) -> 'EntryStream':
        """Fill a fresh stream by handing a sink to `write_into`."""
        backing: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(
            max_size=max_in_memory
        )
        try:
            write_into(backing)
            backing.seek(0)
        except BaseException:
            backing.close()
            raise
        return cls(backing)

    def _check_open(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def _took(self, n: int, *, exhausted: bool) -> None:
        """
        `n` bytes just came out of the backing store.
        """

        # The single hook every way of reading passes through — `read`, `read1`,
        # `readinto`, and everything `io` builds on top of them. Nothing to do
        # here; `_PipeStream` overrides it, and overriding it is all it needs.
        # `exhausted` is true when that read reached the end of the data.

    def read(self, size: int | None = -1) -> bytes:
        """Read up to `size` bytes; all of it when `size` is negative."""
        return self._read_chunk(-1 if size is None else size)

    def read1(self, size: int = -1) -> bytes:
        """
        Read up to `size` bytes with a single call to the backing store.

        Required by `io.BufferedIOBase` so the stream can be wrapped in
        `io.TextIOWrapper` (e.g. to read a text entry line by line).
        """
        return self._read_chunk(size)

    def _read_chunk(self, size: int) -> bytes:
        """
        Read up to `size` bytes with a single call to the backing store.
        """
        self._check_open()
        data = self._backing.read(size)
        # A negative size reads until EOF in one call — that call is itself
        # the end of the data, whether or not it returned any bytes.
        self._took(len(data), exhausted=size < 0 or not data)
        return data

    def readinto(self, buffer: WriteableBuffer) -> int:
        """Read bytes into a pre-allocated buffer, with no copy in between."""
        self._check_open()
        n = self._backing.readinto(buffer)
        self._took(n, exhausted=not n)
        return n

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        """Move the read position."""
        self._check_open()
        return self._backing.seek(pos, whence)

    def tell(self) -> int:
        """Current read position."""
        self._check_open()
        return self._backing.tell()

    def readable(self) -> bool:
        """Always true: entry streams are read-only."""
        return True

    def seekable(self) -> bool:
        """Always true for the default backing."""
        return True

    def close(self) -> None:
        """Close the stream and release its backing storage."""
        if not self.closed:
            self._backing.close()
        super().close()


class _PipeStream(EntryStream):
    """
    An entry stream fed live through an OS pipe by a worker thread.

    Same reading surface as `EntryStream`, three differences that follow from
    the backing being a pipe: it does not rewind, memory stays flat however
    large the entry is, and the first bytes arrive before the last ones are
    decoded. Closing it early is allowed: the writing side then fails on its
    next write and the worker thread ends.

    Unlike `EntryStream.from_writer`, decoding failures here don't surface as
    a raised exception on the worker thread's side — the worker just closes
    its end of the pipe, which the reading side sees as an ordinary end of
    data. To turn that into a loud failure, this stream compares bytes read
    against `expected_size` (the entry's known size) once the data actually
    runs out (an empty read), never on an early `close()`: closing before the
    end is a caller's prerogative, not corruption.
    """

    def __init__(
        self,
        backing: _Backing,
        expected_size: int | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(backing)
        self._expected_size = expected_size
        self._read_total = 0
        # Хранилище, из которого читает рабочий поток, живёт дольше архива:
        # отпускаем его на закрытии стрима, и только один раз.
        self._on_close = on_close

    def _took(self, n: int, *, exhausted: bool) -> None:
        """Count the bytes, and check the tally once the data runs out."""
        self._read_total += n
        if not exhausted:
            return
        expected = self._expected_size
        if expected is not None and expected > 0 and self._read_total < expected:
            raise CorruptArchiveError(
                f"поток оборвался на {self._read_total} из {expected} "
                "ожидаемых байт: распаковка записи не завершилась"
            )

    def seekable(self) -> bool:
        """Always false: a pipe has no positions to go back to."""
        return False

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        """Never succeeds; a pipe cannot rewind."""
        raise io.UnsupportedOperation("stream=True is not seekable")

    def tell(self) -> int:
        """Never succeeds; a pipe has no position."""
        raise io.UnsupportedOperation("stream=True is not seekable")

    def close(self) -> None:
        """Close the pipe and let go of whatever fed it."""
        already_closed = self.closed
        try:
            super().close()
        finally:
            # После `super().close()`: пока читающий конец открыт, рабочий поток
            # может ещё держать архив. И только на первом закрытии — `close()`
            # обязан быть повторяемым, а заявка снимается ровно однажды.
            if not already_closed and self._on_close is not None:
                self._on_close()
