from pathlib import PurePosixPath

import pytest

from newtua._entry import Entry, EntryKind


def make(**kw) -> Entry:
    defaults = dict(
        index=0,
        path=PurePosixPath("docs/a.txt"),
        raw_name=b"docs/a.txt",
        kind=EntryKind.FILE,
        size=8,
        is_encrypted=False,
        mode=0o644,
        mtime=None,
    )
    return Entry(**{**defaults, **kw})


def test_kind_predicates():
    assert make().is_file()
    assert not make().is_dir()
    assert make(kind=EntryKind.DIR).is_dir()
    assert make(kind=EntryKind.SYMLINK).is_symlink()


def test_frozen_and_comparable():
    assert make() == make()
    with pytest.raises(AttributeError):
        make().size = 1


def test_archive_backref_is_not_part_of_equality():
    a, b = make(), make()
    object.__setattr__(a, "_archive", "чужой архив")
    assert a == b


def test_same_name_at_different_positions_are_different_entries():
    """Дубликаты имён в архивах допустимы — различать их обязан index."""
    assert make(index=0) != make(index=1)


def test_read_without_an_archive_is_a_value_error():
    with pytest.raises(ValueError):
        make().read()


def test_extract_of_one_entry_puts_it_straight_into_dest(tmp_path, two_entry_zip):
    """Обёртка для одной записи бессмысленна.

    Раньше `entry.extract(dest)` клал файл в `dest/имя-архива/файл`: папка,
    названная по архиву, появлялась даже когда распаковывали ровно одну запись.
    """
    import newtua

    dest = tmp_path / "out"
    with newtua.Archive(two_entry_zip) as ar:
        ar["a.txt"].extract(dest)

    assert sorted(p.name for p in dest.iterdir()) == ["a.txt"]
    assert (dest / "a.txt").read_bytes() == b"a"


def test_extract_of_one_entry_keeps_its_own_subdirectory(tmp_path):
    """Обёртки нет, а собственный путь записи внутри архива сохраняется."""
    import zipfile

    import newtua

    archive = tmp_path / "nested.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docs/a.txt", b"a")
        zf.writestr("other.txt", b"b")

    dest = tmp_path / "out"
    with newtua.Archive(archive) as ar:
        ar["docs/a.txt"].extract(dest)

    assert (dest / "docs" / "a.txt").read_bytes() == b"a"
