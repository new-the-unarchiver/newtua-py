import newtua._newtua as _newtua
from newtua._format import Format


def test_format_is_a_string_enum():
    assert Format("7z") is Format.SEVENZ
    assert Format.ZIP == "zip"
    assert f"{Format.ZIP}" == "zip"


def test_format_matches_the_engine():
    """Сторож: питоновский enum не должен отставать от FormatId в ядре."""
    from_rust = set(_newtua._all_formats())
    from_python = {f.value for f in Format}
    assert from_python == from_rust, (
        f"только в ядре: {sorted(from_rust - from_python)}; "
        f"только в Python: {sorted(from_python - from_rust)}"
    )
