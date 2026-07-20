import pytest

from newtua._errors import (
    CorruptArchiveError,
    EntryNotFoundError,
    NewtuaError,
    PasswordRequiredError,
    UnknownFormatError,
    raise_for,
)


class _Fake(Exception):
    """Похоже на то, что бросает скомпилированный модуль."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("unknown_format", UnknownFormatError),
        ("encrypted", PasswordRequiredError),
        ("corrupt", CorruptArchiveError),
    ],
)
def test_raise_for_maps_kind(kind, expected):
    with pytest.raises(expected):
        raise_for(_Fake("boom", kind))


def test_io_kind_becomes_builtin_oserror():
    with pytest.raises(OSError):
        raise_for(_Fake("no such file", "io"))


def test_entry_not_found_is_a_keyerror():
    assert issubclass(EntryNotFoundError, KeyError)


def test_everything_else_shares_one_root():
    assert issubclass(UnknownFormatError, NewtuaError)
    assert not issubclass(EntryNotFoundError, NewtuaError)


def test_unknown_kind_falls_back_to_root():
    with pytest.raises(NewtuaError):
        raise_for(_Fake("something new", "kind_from_the_future"))
