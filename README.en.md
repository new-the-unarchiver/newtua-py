# newtua (Python bindings)

[English](README.md) · [Русский](README_ru.md)

Python bindings for [newtua](https://github.com/new-the-unarchiver) — a fast archive extractor written in Rust (a reimagining of The Unarchiver). The package only reads and extracts archive contents: it never creates archives. It understands 55 formats — from zip, 7z, rar and tar to disk images (DMG, ISO, WIM) and legacy formats such as StuffIt and ARJ. All the work happens in-process inside Python, with no external programs launched.

## Installation

```bash
pip install newtua
```

Requires Python 3.11 or newer. The package is typed (`py.typed`) and passes `mypy --strict`, so editor type hints work right away.

## Quick start

```python
import newtua

with newtua.Archive("photos.zip") as ar:
    for entry in ar:
        print(entry.path, entry.size)
    ar.extract("out/")
```

## Opening an archive

`Archive` accepts a file path, ready-made bytes, or an already-open binary file object. The constructor doesn't open anything right away — the actual opening happens on first access to the archive. That's deliberate: it lets you check whether a password is needed before you have to supply one.

```python
import pathlib

import newtua

# a path on disk
with newtua.Archive("archive.7z") as ar:
    print(len(ar))

# bytes — no temporary file
data = pathlib.Path("archive.7z").read_bytes()
with newtua.Archive(data) as ar:
    print(len(ar))

# an already-open file object
with open("archive.7z", "rb") as fh, newtua.Archive(fh) as ar:
    print(len(ar))
```

`Archive` also works without `with`, but then the temporary file (if the source was bytes or a stream) and the open reader have to be closed by hand, via `ar.close()`.

## Iterating and accessing entries

An archive behaves like an ordinary sequence: length, iteration, access by index or by name, slicing, and a membership check.

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    print(len(ar))
    for entry in ar:
        print(entry.path, entry.kind, entry.size, entry.is_encrypted)
    print(ar[0])                # by index
    print(ar["a.txt"])           # by name
    print("a.txt" in ar)         # True
    print(ar[0:1])               # slice — a tuple of entries
```

Each entry is a `newtua.Entry` with the fields `index`, `path`, `raw_name`, `kind`, `size`, `is_encrypted`, `mode`, `mtime`, and its own methods: `entry.is_file()`, `entry.is_dir()`, `entry.is_symlink()`, `entry.read()`, `entry.open()`, `entry.extract(to)`.

`path` is the decoded, cleaned-up path — the one to work with. `raw_name` is `bytes`: the name exactly as stored in the archive, with no decoding applied. Path-safety checks should look at this one, because decoding here is lossy: names in archives aren't required to be UTF-8, and turning them into a string would erase the very thing the check is there to catch. To decode it yourself, use `ar.detected_encoding` (see below).

`entry.extract(to)` places the entry straight into the given folder, with no wrapper folder: a single named entry can't scatter across `to` anyway, and wrapping it would only bury it one level deeper than asked. The entry's own path inside the archive is preserved.

Looking up a name that doesn't exist raises `newtua.EntryNotFoundError` — a subclass of `KeyError`, so code around `ar["name"]` behaves the same as code around a dict. An out-of-range index gives `IndexError`, and touching an already-closed archive gives `ValueError`.

## Reading and extracting

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    data = ar.read(0)            # bytes, whole entry, in memory
    with ar.open("a.txt") as f:   # a newtua.EntryStream file object
        text = f.read()

    report = ar.extract("out/")
    print(report.extracted, report.failed, report.aborted)
```

`ar.open(...)` returns `newtua.EntryStream` — an ordinary binary file object. You can pass it to `shutil.copyfileobj`, feed it to `json.load`, or wrap it in `io.TextIOWrapper` to read a text entry line by line:

```python
import io
import newtua

with newtua.Archive("archive.7z") as ar, ar.open("a.txt") as f:
    for line in io.TextIOWrapper(f, encoding="utf-8"):
        print(line)
```

One caveat about types: `EntryStream` subclasses `io.BufferedIOBase`, not the `typing.BinaryIO` protocol. Those are different things, and a function with an explicit `BinaryIO` annotation (or `IO[bytes]`) won't accept the object — `mypy` will flag an incompatible type, even though at runtime it would read just fine. Annotate your own functions that take this object as `io.BufferedIOBase`, or as `newtua.EntryStream` directly.

### Streaming reads (`stream=True`)

Reading has two modes, and the first one is the default.

**Normal (`stream=False`)** — the entry is fully extracted up front: a small one stays in memory, a large one goes to a temporary file. The result is seekable (`seek`, `tell`), which is what most code expects.

**Streaming (`stream=True`)** — a worker thread extracts the entry straight into an OS pipe, and the caller reads from the other end. There are exactly three differences:

- **Memory stays flat.** Even a gigabyte-sized entry doesn't grow the footprint along with it.
- **The first byte arrives right away**, without waiting for the last byte to be decompressed.
- **No seeking.** `seekable()` returns `False`, and `seek()`/`tell()` raise `io.UnsupportedOperation`.

```python
import hashlib
import newtua

with newtua.Archive("huge.zip") as ar, ar.open("big.iso", stream=True) as f:
    digest = hashlib.sha256()
    while chunk := f.read(1024 * 1024):
        digest.update(chunk)
    print(digest.hexdigest())
```

Reach for this mode when the entry is large and only a single pass over it is needed: computing a hash, streaming it over the network, feeding a streaming parser. For everything else the normal mode is more convenient and stays the default.

A stream can be opened even after the archive itself is closed: `ar.open(..., stream=True)` doesn't return control until the worker thread has opened the archive, so the source can't be pulled out from under it. For the same reason, an opening error (wrong password, corrupt header) arrives as an exception right at the `open` call, not as a read that breaks off midway.

The mode requires POSIX pipes: on Windows it raises `NotImplementedError`.

`ar.extract(to, ...)` returns `newtua.Report` with counts of extracted and failed entries and a cancellation flag. By default (`wrapper=True`), content with no shared root directory is unwrapped into a folder named after the archive — the same behavior as the original The Unarchiver. Pass `wrapper=False` to extract as-is. The `selection` parameter takes a list of indices, names, or entries themselves and extracts only those.

The wrapper folder's name comes from the source's name. An archive opened from raw bytes or from `io.BytesIO` has no name at all — then there's no wrapper, and content lands in `to` as-is. A file object opened from a file has a name, and the wrapper is named after it.

## Password

`needs_password` tells whether an archive is encrypted, so this can be checked before a person is asked for a password. The password can be supplied either right away in the constructor, or later through the `password` property: changing the password simply reopens the archive.

```python
import newtua

ar = newtua.Archive("secret.zip")
ar.password = "неверный"
try:
    ar.read(0)
except newtua.WrongPasswordError:
    ar.password = "правильный"
data = ar.read(0)
ar.close()
```

A missing password where one is required raises `newtua.PasswordRequiredError`; a wrong one raises `newtua.WrongPasswordError`.

## Name encoding

Names inside older archives aren't always UTF-8. `Archive` detects the encoding from the file names on its own, and can report what it guessed:

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    print(ar.detected_encoding)   # e.g. "windows-1251"
```

The verdict is one per archive: the engine decides it at open time, in one pass, from the real bytes of every name. So the same value can be used to decode a raw name too:

```python
import newtua

with newtua.Archive("старый.zip") as ar:
    for entry in ar:
        print(entry.raw_name, "→", entry.raw_name.decode(ar.detected_encoding))
```

The constructor's `encoding=` parameter overrides the guess if it got it wrong; `detected_encoding` then returns exactly the encoding that was set.

## Archive format

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    print(ar.format)              # "7z"
    print(ar.format == "7z")      # True
```

`ar.format` returns `newtua.Format` — a `StrEnum`, so it can be compared directly with a string.

## Progress

```python
import newtua

def show(event: newtua.ProgressEvent) -> None:
    match event:
        case newtua.EntryStarted(path=path, size=size):
            print(f"{path} ({size} байт)")
        case newtua.BytesWritten(written=written):
            pass  # you can draw a progress bar here
        case newtua.EntryFinished():
            print("готово")

with newtua.Archive("big.zip") as ar:
    ar.extract("out/", progress=show)
```

The handler receives one event object at a time — `EntryStarted`, `BytesWritten`, or `EntryFinished`. Return `False` from the handler to cancel extraction midway.

## Errors

All engine errors subclass `newtua.NewtuaError`:

- `UnknownFormatError` — the archive format wasn't recognized.
- `UnsupportedError` — the format is known, but this particular capability isn't supported.
- `PasswordRequiredError` — the archive is encrypted and no password was supplied.
- `WrongPasswordError` — the supplied password didn't work.
- `CorruptArchiveError` — the archive data is corrupted.
- `MissingVolumeError` — a volume is missing from a multi-volume archive.
- `UnsafePathError` — an entry's path would have escaped the extraction folder.

`newtua.EntryNotFoundError` stands apart: it subclasses `KeyError`, not `NewtuaError`, because looking up an entry by name should behave like looking something up in a dict. An out-of-range index gives a plain `IndexError`, and any operation on an already-closed archive gives `ValueError`.

## Testing

An archive can be opened straight from bytes, so a test needs no temporary file, and a progress handler is just an ordinary list, with no wrapper needed.

```python
import pathlib
import newtua
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

def test_extracts_everything(tmp_path):
    data = (FIXTURES / "hello.7z").read_bytes()
    with newtua.Archive(data) as ar:
        report = ar.extract(tmp_path, wrapper=False)
    assert report.extracted == 1
    assert (tmp_path / "a.txt").read_text() == "hello 7z"

def test_collects_progress(tmp_path):
    events: list[newtua.ProgressEvent] = []
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        ar.extract(tmp_path, progress=events.append)
    assert any(isinstance(e, newtua.EntryStarted) for e in events)

def test_unknown_format(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"это точно не архив")
    with pytest.raises(newtua.UnknownFormatError):
        len(newtua.Archive(bad))
```

`tmp_path` is a standard pytest fixture — a fresh temporary folder per test. Exceptions are checked by exact type via `pytest.raises`, not by parsing the message text.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install maturin pytest
cd crates/newtua-py
maturin develop                          # build and install into the venv
python -m pytest tests
```

### Building wheels

`abi3` (the stable ABI, from Python 3.11 on) — one wheel per platform covers every Python ≥ 3.11. Type hints ship inside it: the package itself is typed in code and marked `py.typed`, and the `_newtua.pyi` stub describes the compiled part.

```bash
maturin build --release
```

**Release build (in CI):** wheels for Windows x86_64, macOS arm64 + x86_64, and Linux manylinux x86_64 + aarch64 (musllinux optional), via `maturin-action`/cibuildwheel in GitHub Actions. Dependencies with C code (bzip2, xz, AES for 7z, the vendored libunrar) are built from source, so the build images need a C toolchain (especially for aarch64 cross-builds).

> On the very newest CPython (e.g. 3.14), building the abi3 extension may require `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`, until PyO3 explicitly lists that interpreter as supported.
