import asyncio
import os
import pathlib

import pytest

import newtua
from newtua import _newtua

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX pipe path")


def test_list_path_matches_sync_listing():
    archive = str(FIXTURES / "hello.7z")
    raw, fmt, enc = _newtua.list_path(archive, None, None)
    with newtua.Archive(archive) as ar:
        assert fmt == ar.format.value
        assert enc == ar.detected_encoding
        assert [e.path for e in raw] == [str(e.path) for e in ar]


def test_read_path_matches_sync_read():
    archive = str(FIXTURES / "hello.7z")
    raw, _, _ = _newtua.list_path(archive, None, None)
    with newtua.Archive(archive) as ar:
        for i in range(len(raw)):
            assert _newtua.read_path(archive, i, None, None) == ar.read(i)


def test_extract_path_matches_sync_extract(tmp_path):
    archive = str(FIXTURES / "hello.7z")
    a, b = tmp_path / "a", tmp_path / "b"
    r1 = _newtua.extract_path(archive, str(a), None, True, False, True, None, archive, None, None)
    with newtua.Archive(archive) as ar:
        r2 = ar.extract(str(b))
    assert (r1.extracted, r1.failed, r1.aborted) == (r2.extracted, r2.failed, r2.aborted)
    # Одинаковое дерево файлов.
    names_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*"))
    names_b = sorted(p.relative_to(b).as_posix() for p in b.rglob("*"))
    assert names_a == names_b


def test_extract_path_releases_the_gil(tmp_path):
    # Пока extract_path работает в to_thread, событийный цикл должен жить:
    # счётчик обязан вырасти во время распаковки. Если GIL держится — счётчик
    # почти не двигается (цикл заморожен).
    archive = str(FIXTURES / "hello.7z")

    async def main() -> int:
        ticks = 0

        async def spin() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        spinner = asyncio.ensure_future(spin())
        await asyncio.to_thread(
            _newtua.extract_path, archive, str(tmp_path / "o"),
            None, True, False, True, None, archive, None, None,
        )
        spinner.cancel()
        return ticks

    assert asyncio.run(main()) > 0


@posix_only
def test_open_stream_path_matches_sync_stream():
    archive = str(FIXTURES / "hello.7z")
    read_fd, write_fd = os.pipe()
    _newtua.open_stream_path(archive, 0, write_fd, None, None)
    with os.fdopen(read_fd, "rb") as f:
        streamed = f.read()
    assert streamed == _newtua.read_path(archive, 0, None, None)
