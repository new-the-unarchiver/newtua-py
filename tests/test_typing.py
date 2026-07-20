"""Сторож: заглушка не должна отставать от скомпилированного модуля."""

import newtua._newtua as _newtua


def test_stub_covers_the_module():
    """Всё публичное из _newtua описано в заглушке."""
    stub = (
        __import__("pathlib")
        .Path(_newtua.__file__)
        .parent.joinpath("_newtua.pyi")
        .read_text()
    )
    for name in dir(_newtua):
        if name.startswith("__"):
            continue
        assert name in stub, f"нет в _newtua.pyi: {name}"
