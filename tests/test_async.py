import asyncio
import pathlib

import pytest

import newtua

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


def test_async_listing_matches_sync():
    async def main():
        async with newtua.AsyncArchive(str(FIXTURES / "hello.7z")) as ar:
            return ar.format, ar.detected_encoding, [str(e.path) for e in ar], len(ar)

    fmt, enc, paths, n = run(main())
    with newtua.Archive(str(FIXTURES / "hello.7z")) as sync:
        assert fmt == sync.format
        assert enc == sync.detected_encoding
        assert paths == [str(e.path) for e in sync]
        assert n == len(sync)


def test_async_read_matches_sync():
    async def main():
        async with newtua.AsyncArchive(str(FIXTURES / "hello.7z")) as ar:
            return await ar.read(0)

    with newtua.Archive(str(FIXTURES / "hello.7z")) as sync:
        assert run(main()) == sync.read(0)


def test_async_extract_matches_sync(tmp_path):
    async def main():
        async with newtua.AsyncArchive(str(FIXTURES / "hello.7z")) as ar:
            return await ar.extract(str(tmp_path / "a"))

    report = run(main())
    with newtua.Archive(str(FIXTURES / "hello.7z")) as sync:
        expected = sync.extract(str(tmp_path / "b"))
    assert (report.extracted, report.failed, report.aborted) == (
        expected.extracted, expected.failed, expected.aborted
    )


def test_async_entries_are_metadata_only():
    # Записи из AsyncArchive не должны тащить блокирующий sync-вызов.
    async def main():
        async with newtua.AsyncArchive(str(FIXTURES / "hello.7z")) as ar:
            return ar[0]

    entry = run(main())
    with pytest.raises(ValueError, match="not attached"):
        entry.read()
