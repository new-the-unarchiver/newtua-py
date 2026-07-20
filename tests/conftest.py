"""Fixtures shared across the test modules."""

import pathlib
import zipfile

import pytest


def make_two_entry_zip(path: pathlib.Path) -> pathlib.Path:
    """Zip без общего корня — только такой и заворачивается в папку-обёртку.

    Две записи, обе в корне: общего верхнего каталога нет, поэтому распаковка
    с `wrapper=True` обязана завести папку по имени архива.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", b"a")
        zf.writestr("b.txt", b"b")
    return path


@pytest.fixture
def two_entry_zip(tmp_path: pathlib.Path) -> pathlib.Path:
    """Готовый `multi.zip` с записями `a.txt` и `b.txt`."""
    return make_two_entry_zip(tmp_path / "multi.zip")
