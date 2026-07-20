"""Пользуется всем публичным API — контрольный образец для mypy --strict.

Появится дыра в типах — падает этот файл, а не пользователь.
"""

from __future__ import annotations

import datetime
import pathlib
import tempfile
from pathlib import PurePosixPath

import newtua

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "hello.7z"

version: str = newtua.__version__


def use_everything() -> None:
    with newtua.Archive(FIXTURE, password=None, encoding=None) as ar:
        count: int = len(ar)
        fmt: newtua.Format = ar.format
        needs: bool = ar.needs_password
        charset: str = ar.detected_encoding
        ar.password = "secret"

        first: newtua.Entry = ar[0]
        by_name: newtua.Entry = ar["a.txt"]
        head: tuple[newtua.Entry, ...] = ar[:2]
        present: bool = "a.txt" in ar

        for entry in ar:
            name: PurePosixPath = entry.path
            # `raw_name` — именно `bytes`: имя в архиве не обязано быть UTF-8,
            # и декодировать его здесь значило бы потерять то, ради чего поле
            # вообще есть. Расшифровать при желании можно самому.
            raw: bytes = entry.raw_name
            decoded: str = raw.decode(charset, "replace")
            kind: newtua.EntryKind = entry.kind
            size: int = entry.size
            mode: int | None = entry.mode
            when: datetime.datetime | None = entry.mtime
            a_dir: bool = entry.is_dir()
            a_link: bool = entry.is_symlink()
            if entry.is_file():
                payload: bytes = entry.read()
                with entry.open() as fh:
                    chunk: bytes = fh.read(16)
                stream: newtua.EntryStream = entry.open()
                with stream:
                    piece: bytes = stream.read1(4)
                    into = bytearray(4)
                    got: int = stream.readinto(into)
                    pos: int = stream.seek(0)
                    at: int = stream.tell()
                # Второй режим чтения: распаковка на лету, без перемотки.
                live: newtua.EntryStream = entry.open(stream=True)
                with live:
                    streamed: bytes = live.read(4)
                    rewindable: bool = live.seekable()
                with tempfile.TemporaryDirectory() as one:
                    entry.extract(one)
                print(
                    name, raw, decoded, kind, size, mode, when, a_dir, a_link,
                    len(payload), len(chunk), len(piece), got, pos, at,
                    len(streamed), rewindable,
                )

        data: bytes = ar.read("a.txt")
        report: newtua.Report = ar.extract(FIXTURE.parent, progress=on_event)
        extracted: int = report.extracted
        failed: int = report.failed
        aborted: bool = report.aborted
        print(count, fmt, needs, charset, first, by_name, head, present, len(data))
        print(extracted, failed, aborted)

    # Без `with` архив закрывают руками.
    plain = newtua.Archive(FIXTURE)
    with plain.open(0, stream=True) as live_again:
        print(len(live_again.read()))
    plain.close()


def on_event(event: newtua.ProgressEvent) -> bool | None:
    kind: newtua.EventKind = event.kind
    print(kind)
    match event:
        case newtua.EntryStarted(path=p, size=n):
            print(p.name, n)
        case newtua.BytesWritten(written=w):
            print(w)
        case newtua.EntryFinished(index=i):
            print(i)
    return None


def handle_errors() -> None:
    try:
        newtua.Archive("x.zip").read(0)
    except newtua.PasswordRequiredError:
        pass
    except newtua.WrongPasswordError:
        pass
    except newtua.EntryNotFoundError:
        pass
    except newtua.UnknownFormatError:
        pass
    except newtua.UnsupportedError:
        pass
    except newtua.CorruptArchiveError:
        pass
    except newtua.MissingVolumeError:
        pass
    except newtua.UnsafePathError:
        pass
    except newtua.NewtuaError:
        pass
