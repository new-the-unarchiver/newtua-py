import pathlib

import newtua
from newtua import _newtua

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


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
