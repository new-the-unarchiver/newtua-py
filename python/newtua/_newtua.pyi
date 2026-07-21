"""Type stubs for the compiled module. Private: use the `newtua` package."""

from typing import Any, Callable, Sequence

__version__: str

class NewtuaError(Exception):
    kind: str

class Entry:
    path: str
    raw_name: bytes
    kind: str
    size: int
    is_encrypted: bool
    mode: int | None
    mtime: float | None

class Report:
    extracted: int
    failed: int
    aborted: bool

class Archive:
    def entries(self) -> list[Entry]: ...
    def read(self, index: int) -> bytes: ...
    def write_entry_to(self, index: int, sink: Any) -> None: ...
    # Unix only: takes ownership of `write_fd` and closes it when the entry ends.
    # Blocks until the worker thread has opened the archive, so an open failure
    # raises here. Takes ownership of `write_fd` only when it returns normally.
    def open_stream(self, index: int, write_fd: int) -> None: ...
    # Windows only (present just in the Windows build): creates the pipe on the
    # Rust side and returns the read HANDLE for the caller to adopt via
    # `msvcrt.open_osfhandle`. Blocks until the worker has opened the archive.
    def open_stream_windows(self, index: int) -> int: ...
    def format(self) -> str: ...
    def detected_encoding(self) -> str: ...
    def extract(
        self,
        dest: str,
        selection: Sequence[int] | None = ...,
        wrapper: bool = ...,
        strict: bool = ...,
        preserve: bool = ...,
        progress: Callable[[str, int, str | None, int, int], bool | None] | None = ...,
        # None means "no wrapper folder": the engine substitutes no path of
        # its own, because only the Python layer knows where the archive
        # came from.
        name_source: str | None = ...,
    ) -> Report: ...

def open(
    path: str, password: str | None = ..., encoding: str | None = ...
) -> Archive: ...
def list_path(
    path: str, password: str | None = ..., encoding: str | None = ...
) -> tuple[list[Entry], str, str]: ...
def read_path(
    path: str, index: int, password: str | None = ..., encoding: str | None = ...
) -> bytes: ...
def _all_formats() -> list[str]: ...
