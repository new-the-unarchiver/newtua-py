"""Тот же набор проверок, что был до перехода на новый API.

Держится отдельно от test_archive.py намеренно: это перевод исходных шести
случаев один в один, чтобы видеть, что переход ничего не потерял.
"""

import pathlib

import pytest

import newtua

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HELLO_7Z = FIXTURES / "hello.7z"


def test_open_list_and_read():
    with newtua.Archive(HELLO_7Z) as ar:
        assert len(ar) == 1
        entries = list(ar)
        assert str(entries[0].path) == "a.txt"
        assert entries[0].kind is newtua.EntryKind.FILE
        assert entries[0].size == 8
        assert entries[0].is_encrypted is False
        assert ar.read(0) == b"hello 7z"


def test_extract(tmp_path):
    with newtua.Archive(HELLO_7Z) as ar:
        report = ar.extract(tmp_path, wrapper=False)
    assert report.extracted == 1
    assert report.aborted is False
    assert (tmp_path / "a.txt").read_bytes() == b"hello 7z"


def test_progress_callback(tmp_path):
    seen: list[newtua.ProgressEvent] = []
    with newtua.Archive(HELLO_7Z) as ar:
        ar.extract(tmp_path, wrapper=False, progress=seen.append)
    assert any(isinstance(e, newtua.EntryStarted) for e in seen)


def test_unknown_format_raises(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"not an archive at all, definitely not")
    with pytest.raises(newtua.UnknownFormatError):
        len(newtua.Archive(bad))


def test_version():
    assert isinstance(newtua.__version__, str)
    assert newtua.__version__


def test_public_surface():
    for name in newtua.__all__:
        assert hasattr(newtua, name), f"missing public name: {name}"
    assert issubclass(newtua.NewtuaError, Exception)
    assert issubclass(newtua.EntryNotFoundError, KeyError)
