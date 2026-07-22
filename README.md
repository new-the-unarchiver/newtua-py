# [New The Unarchiver](https://github.com/new-the-unarchiver) — archive extractor, Python edition

> 🇷🇺 **По-русски:** [README_ru.md](README_ru.md)

- [New The Unarchiver — archive extractor, Python edition](#new-the-unarchiver--archive-extractor-python-edition)
  - [Why New The Unarchiver](#why-new-the-unarchiver)
  - [Installation](#installation)
  - [Quick start](#quick-start)
  - [`Archive` — synchronous access to a single archive](#archive--synchronous-access-to-a-single-archive)
    - [Archive properties](#archive-properties)
    - [The archive as a sequence](#the-archive-as-a-sequence)
      - [Exceptions](#exceptions)
    - [Archive methods](#archive-methods)
    - [Extraction](#extraction)
    - [Passwords](#passwords)
    - [Name encoding](#name-encoding)
    - [Archive format](#archive-format)
    - [Extraction progress](#extraction-progress)
  - [`Entry` — a single archive entry](#entry--a-single-archive-entry)
    - [Fields](#fields)
    - [Entry methods](#entry-methods)
  - [`EntryStream` — an entry as a file object](#entrystream--an-entry-as-a-file-object)
    - [Reading on the fly (`stream=True`)](#reading-on-the-fly-streamtrue)
  - [Concurrency and async](#concurrency-and-async)
    - [Many archives: `extract_many` / `list_many`](#many-archives-extract_many--list_many)
    - [`AsyncArchive` — an archive under asyncio](#asyncarchive--an-archive-under-asyncio)
    - [Threads, processes, gevent — what you get and what to watch for](#threads-processes-gevent--what-you-get-and-what-to-watch-for)
  - [Windows limitations](#windows-limitations)
  - [Exceptions](#exceptions-1)
  - [Testing](#testing)
  - [Development](#development)
    - [Building wheels](#building-wheels)


**[New The Unarchiver](https://github.com/new-the-unarchiver)** is a convenient and capable archive extractor. Its engine is written in Rust, with bindings for several languages layered on top. This is `newtua` — the package for **Python**.

The package only reads and extracts; it **never creates** archives.

The engine currently supports 55 formats — from zip, 7z, rar and tar to disk images (DMG, ISO, WIM) and older formats such as StuffIt and ARJ. More may follow.

Everything runs inside your Python process: no external binaries, no helper processes.

## Why [New The Unarchiver](https://github.com/new-the-unarchiver)

- **Format-agnostic.** The same code opens any of the 55 formats. The format is detected by content, not by file extension.
- **Cross-platform.** The only differences are the ones the platforms themselves impose, and even those are kept to a minimum.
- **Fast.** A Rust engine, extraction entirely in-process, and no intermediate files where they are not needed.
- **Concurrency built in.** Extraction releases the GIL, so several archives really can be unpacked in parallel. Ready-made tools for threads, processes, `asyncio` and even `gevent` come with the package.
- **Made for Python developers.** An idiomatic API: an archive is a sequence, an entry is a file object, and errors are typed exceptions rather than return codes.
- **Typed.** The package is marked `py.typed` and passes `mypy --strict` — your IDE picks up the types and your CI does not trip over them.

## Installation

```bash
uv add newtua
```

Python 3.11 or newer is required. The package ships as prebuilt `abi3` wheels (one wheel per platform covers every Python ≥ 3.11), so installing needs neither Rust nor a C compiler.

## Quick start

```python
import newtua

# Print the contents of an archive and extract it into `out/`
with newtua.Archive("photos.zip") as ar:
    for entry in ar:
        print(entry.path, entry.size)
    ar.extract("out/")

# Read a single entry fully into memory
with newtua.Archive("archive.7z") as ar:
    data = ar.read("readme.txt")

# Extract a batch of archives in parallel, across cores, into separate folders
results = newtua.extract_many(
    [("a.zip", "out/a"), ("b.7z", "out/b")],
    on_result=lambda job, report: print(job.archive, report.extracted),
)
```

---

## `Archive` — synchronous access to a single archive

The main class. Use it to read and extract archives.

```python
newtua.Archive(source, *, password=None, encoding=None)
```

`source` is a file path (`str` or `os.PathLike`), a `bytes` object, or an already opened binary file object. The constructor reads nothing right away: the archive is really opened on first access. That is deliberate — it lets you first check whether a password is needed and only then set it. If you already know the password, pass it straight to the constructor.

```python
import pathlib
import newtua

# From a path on disk
with newtua.Archive("archive.7z") as ar:
    print(len(ar))

# From bytes, with no temporary file on your side
data = pathlib.Path("archive.7z").read_bytes()
with newtua.Archive(data) as ar:
    print(len(ar))

# From an already opened file object
with open("archive.7z", "rb") as f, newtua.Archive(f) as ar:
    print(len(ar))
```

`Archive` also works without `with`, but then you have to close it yourself via `ar.close()`. That removes the temporary file (if the source was bytes or a stream) and releases the resources it held.

### Archive properties

| Property | Type | What it is |
|---|---|---|
| `format` | `newtua.Format` | The archive format, e.g. `"7z"` or `newtua.Format.RAR`. It is a `StrEnum`, with everything that implies. |
| `needs_password` | `bool` | Whether a password is required to read the contents. You can check this before asking the user for one. |
| `password` | `str \| None` | The current password. Assigning to it reopens the archive with the new password. |
| `detected_encoding` | `str` | The encoding the engine detected for file names (e.g. `"windows-1251"`). |

### The archive as a sequence

> From here on we say "entry" rather than "file" when talking about archive contents, because an entry may also be a directory or a symbolic link.

`Archive` behaves like an ordinary Python sequence:

```python
with newtua.Archive("archive.7z") as ar:
    len(ar)                 # number of entries
    for entry in ar: ...    # iterate over entries
    ar[0]                   # entry by index
    ar["a.txt"]             # entry by name (the first one with that name — names may repeat)
    "a.txt" in ar           # is there such an entry
    ar[0:10]                # a slice — a tuple of entries
```

#### Exceptions

- Looking up a name that does not exist raises `EntryNotFoundError` (a subclass of `KeyError`, since `ar["name"]` behaves like a dictionary lookup).
- A numeric index out of range raises `IndexError` (standard for a `Sequence`).
- Touching an already closed archive raises `ValueError` (as with a closed file).

### Archive methods

```python
ar.read(entry) -> bytes
```
Read a single entry fully into memory. `entry` is an index, a name, a path, or an `Entry` itself.

```python
ar.open(entry, *, stream=False) -> EntryStream
```
Open an entry as a file object (see [EntryStream](#entrystream--an-entry-as-a-file-object)).

```python
ar.extract(
    dest, *, selection=None, wrapper=True, strict=False, preserve=True, progress=None
) -> newtua.Report
```
Extract entries into the `dest` folder. The parameters and the return value are described below.

```python
ar.close()
```
Close the archive and remove the temporary file, if one was created.

### Extraction

```python
with newtua.Archive("archive.7z") as ar:
    report = ar.extract("out/")
    print(report.extracted, report.failed, report.aborted)
```

`extract` returns a `newtua.Report` — the number of entries extracted (`extracted`) and not extracted (`failed`), plus a cancellation flag (`aborted`).

Parameters of `extract`:

- `selection` — a list of indices, names, or entries: extract only those.
- `wrapper` (default `True`) — contents without a shared top-level directory get unpacked into a folder named after the archive (as the original The Unarchiver did). `wrapper=False` extracts them as they are. The wrapper name comes from the source name; an archive built from raw bytes or an `io.BytesIO` has no name, so it gets no wrapper.
- `strict` — stop extraction at the first unsafe entry (a path that would escape `dest`) instead of skipping it.
- `preserve` — restore file permissions and timestamps where the archive records them.
- `progress` — a progress handler (see [Extraction progress](#extraction-progress)).

### Passwords

`needs_password` tells you whether the archive is encrypted. The password goes either straight into the constructor or later through the `password` property — changing it simply reopens the archive.

```python
ar = newtua.Archive("secret.zip")
if ar.needs_password:
    ar.password = "secret"
data = ar.read(0)
ar.close()
```

A missing password where one is required raises `PasswordRequiredError`; a wrong password raises `WrongPasswordError`.

### Name encoding

Names in older archives are not always UTF-8. `Archive` detects the encoding itself — one verdict for the whole archive, taken at once from the actual bytes of every name:

```python
with newtua.Archive("old.zip") as ar:
    print(ar.detected_encoding)          # "windows-1251"
    for entry in ar:
        print(entry.raw_name.decode(ar.detected_encoding))
```

The `encoding=` constructor parameter overrides autodetection when it gets things wrong; `detected_encoding` then returns the value you set.

### Archive format

```python
with newtua.Archive("archive.7z") as ar:
    print(ar.format)                           # "7z"
    print(ar.format == newtua.Format.RAR)      # False
    print(ar.format == "7z")                   # True — Format is a StrEnum
```

### Extraction progress

`extract(..., progress=...)` calls the handler once per event:

```python
def show(event: newtua.ProgressEvent) -> None:
    match event:
        case newtua.EntryStarted(path=path, size=size):
            print(f"{path} ({size} bytes)")
        case newtua.BytesWritten(written=written):
            pass  # update a progress bar or other indicator
        case newtua.EntryFinished():
            print("done")

with newtua.Archive("big.zip") as ar:
    ar.extract("out/", progress=show)
```

The events are `EntryStarted`, `BytesWritten` and `EntryFinished` (all inherit `ProgressEvent`).

Return `False` from the handler to cancel extraction midway: it stops on the current entry and skips the rest.

---

## `Entry` — a single archive entry

An `Entry` holds the immutable metadata of one entry, plus convenience methods to read or extract just that one. You get an `Entry` by iterating over the archive or by index/name/path.

### Fields

| Field | Type | What it is |
|---|---|---|
| `index` | `int` | Position in the archive. This is what identifies an entry unambiguously, since names may repeat. |
| `path` | `path.PurePosixPath` | The decoded, normalized path — the one you actually work with. |
| `raw_name` | `bytes` | The name exactly as the archive stores it, undecoded. |
| `kind` | `newtua.EntryKind` | `FILE`, `DIR` or `SYMLINK` (a `StrEnum` underneath). |
| `size` | `int` | Size in bytes. |
| `is_encrypted` | `bool` | Whether this entry is encrypted. |
| `mode` | `int \| None` | Unix permissions, where applicable. |
| `mtime` | `datetime \| None` | Modification time, if recorded. |

`raw_name` is what path-safety checks should look at: decoding could distort the name, since an archive is not obliged to store it in UTF-8. To decode it yourself, use `entry.raw_name.decode(ar.detected_encoding)`.

### Entry methods

```python
entry.is_file() -> bool
entry.is_dir() -> bool
entry.is_symlink() -> bool
```
Check the entry type.

```python
entry.read() -> bytes
```
Read this entry in full (the same as `ar.read(entry)`). Note that `read()` returns bytes, whereas `open()` returns a file object.

```python
entry.open(*, stream=False) -> newtua.EntryStream
```
Open it as a file object (see [EntryStream](#entrystream--an-entry-as-a-file-object)).

```python
entry.extract(dest) -> newtua.Report
```
Extract only this entry into `dest`, **without** a wrapper folder (there is only one entry). The entry's own path inside the archive is preserved.

> **A note on async.** Entries obtained from an `AsyncArchive` carry metadata only. Their `read()`, `open()` and `extract()` methods do not work — each raises `ValueError`. Read such entries through the `AsyncArchive` itself (`await ar.read(entry)`), so that no blocking operation ends up running inside the event loop. See [Concurrency and async](#concurrency-and-async) for details.

---

## `EntryStream` — an entry as a file object

`ar.open(...)` returns an `EntryStream` — an ordinary binary file object (a subclass of `io.BufferedIOBase`). You can hand it to `shutil.copyfileobj` or `json.load`, or wrap it in an `io.TextIOWrapper` to read a text entry line by line:

```python
import io, newtua

with newtua.Archive("archive.7z") as ar, ar.open("a.txt") as f:
    for line in io.TextIOWrapper(f, encoding="utf-8"):
        print(line)
```

> **A note on typing.** `EntryStream` inherits `io.BufferedIOBase`, not the `typing.BinaryIO` protocol. Annotating it as `BinaryIO` (or `IO[bytes]`) makes `mypy` complain even though the code works. Annotate your own functions as `io.BufferedIOBase` or as `newtua.EntryStream` directly.

### Reading on the fly (`stream=True`)

Reading has two modes — buffered and on the fly. Buffered is the default.

**Buffered (`stream=False`).** The entry is extracted in full up front: a small one stays in memory, a large one goes to a temporary file. The result is seekable (`seek`, `tell`), which is what most code expects.

**On the fly (`stream=True`).** A worker thread extracts the entry straight into an OS pipe, and you read from the other end.

Three differences from the buffered mode:

- **Memory stays constant** no matter how big the entry is — even 100 GB.
- **The first byte arrives immediately**, without waiting for the last one to be extracted.
- **There is no seeking** — `seekable()` returns `False`, and `seek()`/`tell()` raise `io.UnsupportedOperation`.

Example: computing a SHA-256 digest of a large entry on the fly.

```python
import hashlib, newtua

with newtua.Archive("huge.zip") as ar, ar.open("big.iso", stream=True) as f:
    digest = hashlib.sha256()
    while chunk := f.read(1024 * 1024):
        digest.update(chunk)
    print(digest.hexdigest())
```

Use this mode for a large entry you need to pass over exactly once: hashing it, piping it to the network, feeding it to a streaming parser. For everything else the buffered mode is more convenient.

Two more conveniences of this mode:

- **Closing the archive does not break the stream.** `open(..., stream=True)` does not return instantly: it waits until the worker thread has opened the archive, and only then hands you the stream. By that point the worker holds the archive open on its own — so you may call `ar.close()` right away and still read the stream to the end without disturbing it.
- **An opening error surfaces immediately.** If the archive cannot be opened — wrong password, corrupt header — `open(..., stream=True)` raises the exception right at that call. You will not end up in a situation where the stream appears to open, reading starts, and then it breaks off halfway with no explanation.

**On Windows** this mode works only for a **path** source: Rust creates the pipe itself and Python reads from it. A `bytes` source or a file object raises `NotImplementedError` (see [Windows limitations](#windows-limitations)).

---

## Concurrency and async

We wrote the Rust layer specifically so that extraction releases the GIL — which is what makes real parallelism possible.

We also built two ready-made toolsets, so that you do not have to deal with threads, channels and passing objects between them yourself.

### Many archives: `extract_many` / `list_many`

```python
newtua.extract_many(
    jobs, *, backend=newtua.Backend.THREAD, max_workers=None, on_result=None, on_error=None, cancel=None,
) -> list[newtua.BatchResult]
```

`extract_many` extracts several archives in parallel. `jobs` is either `(archive, dest)` tuples or `newtua.ExtractJob` objects for finer control:

```python
import newtua

jobs = [
    ("a.zip", "out/a"),
    newtua.ExtractJob(archive="b.7z", dest="out/b", password="secret"),
]
results = newtua.extract_many(jobs, on_result=lambda job, r: print(job.archive, r.extracted))
for res in results:
    if res.error is None:
        print(res.job.archive, "→", res.report.extracted)
    else:
        print(res.job.archive, "failed:", res.error)
```

```python
newtua.ExtractJob(archive, dest, *, password=None, encoding=None, selection=None, wrapper=True, strict=False, preserve=True, progress=None)
```
One job with its own options. The parameters are the same as for [Archive.extract](#extraction).

```python
newtua.BatchResult(job, report, error)
```
The result of a single job: it carries either a `report` or an `error`. A batch **never fails on the first error** — one broken archive does not stop the others from being extracted, and its exception is available in `error`.

```python
newtua.list_many(
    archives, *, password=None, encoding=None, backend=newtua.Backend.THREAD, max_workers=None, on_result=None, on_error=None,
) -> list[newtua.ListingResult]
```

`list_many` reads the table of contents of several archives in parallel. `archives` is a list of archive paths:

```python
for res in newtua.list_many(["a.zip", "b.7z", "c.rar"]):
    if res.error is None:
        print(res.archive, len(res.entries))
```

`newtua.ListingResult(archive, entries, error)` carries either `entries` or an `error`.

### `AsyncArchive` — an archive under asyncio

```python
newtua.AsyncArchive(source, *, password=None, encoding=None)
```

This is the same `Archive`, except that all the work with bytes happens outside the event loop, so extraction never blocks it. There is no `stream` parameter on `.open`.

```python
async with newtua.AsyncArchive("big.dmg") as ar:
    print(ar.format, len(ar))            # metadata — synchronous, no await
    for entry in ar:                     # iterating entries is synchronous too
        print(entry.path, entry.size)

    data = await ar.read("readme.txt")           # reading — outside the event loop
    report = await ar.extract("out/")            # extraction — outside the event loop

    async with ar.open("payload.bin") as stream:  # streaming read
        async for chunk in stream:
            ...
```

How `AsyncArchive` works:

1. **Metadata is read synchronously.** `format`, `detected_encoding`, `needs_password`, `len(ar)`, iteration, `ar[0]`, `ar["name"]`, `"name" in ar`, slices — all of this is pure Python over a cache, so no `await` is needed. You only await actual work with bytes.
2. **`await ar.read(entry) -> bytes`** and **`await ar.extract(dest, *, selection=None, wrapper=True, strict=False, preserve=True, progress=None) -> Report`** look just like their `Archive` counterparts, but are called with `await`.
3. **`ar.open(entry)`** is an async context manager returning an `AsyncEntryStream` (a streaming read at constant memory). Use it as `await stream.read(n)`, `async for chunk in stream`, `await stream.aclose()`.
4. **There are no blocking methods.** Every blocking method of `Archive` is either replaced by its async counterpart or removed. `AsyncArchive` simply has no synchronous `read`/`extract` — only the `await` versions. A type checker catches the mistake before you even run the code.

### Threads, processes, gevent — what you get and what to watch for

- **Threads** (`Backend.THREAD`, and `asyncio` via `to_thread`) are the simplest form of parallelism here. Extraction runs in real threads without holding the GIL. The rule: **one archive, one thread** (a reader object is bound to its own thread); independent archives in different threads, on the other hand, can be as many as you like.
- **Processes** (`Backend.PROCESS`) give full isolation: a crash in one process does not take the others down. This backend has three traits worth knowing about in advance:
    - **`Archive`/`AsyncArchive` cannot be sent to another process.** An open `Archive`/`AsyncArchive` cannot cross a process boundary — trying to do so raises `TypeError`. That is why `extract_many` accepts **paths only**, and each archive is opened inside its own worker.
    - **`progress` is unavailable.** A callback cannot be sent across a process boundary, so a per-job `progress` is rejected under this backend with a `ValueError`. If you need progress, use threads.
    - **Child processes start via `spawn`.** What does that mean in practice? On macOS and Windows the Python interpreter starts afresh in the child process and therefore **re-imports your module** to get the worker's code. So your code must not run at import time — otherwise the import in the child would run it again, and again. In a normal application this rarely comes up: you call `extract_many(...)` from inside some function or method, not at import time, and everything works. But **in self-running scripts**, where the pool call sits directly in the file body, it must be placed under `if __name__ == "__main__":` to avoid the infinite-recursion trap.
- **gevent** — if you have called `gevent.monkey.patch_all()`, `extract_many` notices and **transparently** switches to `gevent.threadpool` (real OS threads that the hub waits on cooperatively). Nothing is required on your side — the cooperative threads simply work and do not freeze the hub.
- **Progress arrives from a worker thread.** The `progress` callback (on `Archive.extract`, `AsyncArchive.extract`, and on an individual `ExtractJob`) is called from a worker thread, not from the event loop. To reach the event loop from there, use `loop.call_soon_threadsafe`.

---

## Windows limitations

- **Streaming reads (`stream=True`)** work only for a **path source**. A `bytes` source (or a file object) raises `NotImplementedError`. The reason: such a source has to go through a temporary file, and Windows will not let it be deleted while the worker thread holds the archive open. Open the archive by path, or use `stream=False`. In practice this rarely bites: `stream=True` exists for large entries you would rather not hold in memory (or that would not fit at all), and reading such a source into memory first defeats the purpose — so the limitation is more theoretical than practical.
- **`Backend.PROCESS`** on Windows uses `spawn`, with the same caveats described above under [Concurrency](#threads-processes-gevent--what-you-get-and-what-to-watch-for). One more detail: job arguments must survive **`pickle` serialization**. Paths and `ExtractJob` pickle without trouble, so ordinary code is unaffected.

Everything else — the synchronous API, `AsyncArchive`, the threaded `extract_many` — works on Windows exactly as it does on *nix.

## Exceptions

Every error except `EntryNotFoundError` inherits `newtua.NewtuaError`:

| Exception | When |
|---|---|
| `UnknownFormatError` | The archive format was not recognized. |
| `UnsupportedError` | The format is known, but this particular capability is not supported. |
| `PasswordRequiredError` | The archive is encrypted and no password was given. |
| `WrongPasswordError` | The password given did not fit. |
| `CorruptArchiveError` | The archive data is damaged. |
| `MissingVolumeError` | A volume of a multi-volume archive is missing. |
| `UnsafePathError` | An entry's path would escape the extraction folder. |

`EntryNotFoundError` inherits `KeyError` rather than `NewtuaError`, so that lookup by name behaves like a dictionary lookup. A numeric index out of range gives `IndexError`, and an operation on a closed archive gives `ValueError`.

Under `Backend.PROCESS` you get exactly the same typed exceptions as usual: the library converts the engine's error to the right class inside the worker process, so what reaches you is, say, an `UnknownFormatError` and not some opaque serialization failure.

## Testing

We made a point of keeping tests short:

- **An archive opens straight from bytes** — so a test needs no temporary file.
- **A progress handler is just a list** (`events.append`), with no wrappers around it.
- **Exceptions are checked by exact type** via `pytest.raises`, not by parsing message text.

```python
import pathlib, newtua, pytest

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
    bad.write_bytes(b"definitely not an archive")
    with pytest.raises(newtua.UnknownFormatError):
        len(newtua.Archive(bad))
```

`tmp_path` is the standard pytest fixture — a separate temporary folder per test.

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install maturin pytest mypy ruff
maturin develop                          # build and install into the venv
python -m pytest
```

The checks to run: `python -m pytest`, `mypy`, `ruff check python tests`, `cargo fmt --all --check`, `cargo clippy --all-targets -- -D warnings`.

### Building wheels

`abi3` (the stable ABI from Python 3.11 onward) means one wheel per platform covers every Python ≥ 3.11 there. Type hints are included: the package is typed in code and marked `py.typed`, and the `_newtua.pyi` stub describes the compiled part.

```bash
maturin build --release
```

**Release builds** run from [`.github/workflows/wheels.yml`](.github/workflows/wheels.yml), triggered by a `v*` tag or by hand. It builds wheels for Linux manylinux x86_64 + aarch64, macOS arm64 + x86_64 and Windows x86_64, plus an sdist; then it installs each wheel and runs the suite against it on Python 3.11 and 3.13 — one abi3 wheel is supposed to cover both, and that step is what proves it does. Publishing to PyPI happens only on a tag, over trusted publishing (no API token).

The dependencies with C code (bzip2, xz, AES for 7z, libunrar) are built from source, so a build image needs a C toolchain — which matters most when cross-building to aarch64. The workflow sets `LZMA_API_STATIC=1` for the same reason: without it `lzma-sys` links whichever liblzma the build host happens to have, and the wheel ends up needing a library the user may not have.

> On the very newest CPython (3.14, say) building an abi3 extension may require `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` until PyO3 lists that interpreter as supported.
