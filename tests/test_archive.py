import gc
import pathlib
import tempfile
from pathlib import PurePosixPath

import pytest

import newtua
from newtua import Archive, Entry, EntryKind, EntryStarted, Format
from newtua._archive import _safe_kind, _safe_mtime

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HELLO_7Z = FIXTURES / "hello.7z"


def _newtua_tempfiles() -> set[pathlib.Path]:
    """Temp files `Archive` may have spilled bytes/streams into."""
    return set(pathlib.Path(tempfile.gettempdir()).glob("*.newtua"))


def test_construction_does_not_open_anything():
    """Ленивость: несуществующий файл не падает до первого обращения."""
    ar = Archive("нет-такого-файла.zip")
    with pytest.raises(FileNotFoundError):
        len(ar)


def test_sequence_protocol():
    with Archive(HELLO_7Z) as ar:
        assert len(ar) == 1
        assert [e.path for e in ar] == [PurePosixPath("a.txt")]
        assert isinstance(ar[0], Entry)
        assert ar["a.txt"].size == 8
        assert isinstance(ar[:1], tuple)
        assert "a.txt" in ar


def test_missing_index_and_name():
    with Archive(HELLO_7Z) as ar:
        with pytest.raises(IndexError):
            ar[999]
        with pytest.raises(KeyError):
            ar["нет-такого"]


def test_reads_by_name_index_and_entry():
    with Archive(HELLO_7Z) as ar:
        assert ar.read(0) == b"hello 7z"
        assert ar.read("a.txt") == b"hello 7z"
        assert ar.read(ar[0]) == b"hello 7z"
        assert ar["a.txt"].read() == b"hello 7z"


def test_open_gives_a_file_object():
    with Archive(HELLO_7Z) as ar, ar.open("a.txt") as f:
        assert f.read() == b"hello 7z"


def test_accepts_bytes_as_a_source():
    with Archive(HELLO_7Z.read_bytes()) as ar:
        assert ar.read(0) == b"hello 7z"


def test_extract_and_report(tmp_path):
    with Archive(HELLO_7Z) as ar:
        report = ar.extract(tmp_path, wrapper=False)
    assert report.extracted == 1
    assert report.aborted is False
    assert (tmp_path / "a.txt").read_bytes() == b"hello 7z"


def test_progress_callback_takes_one_object(tmp_path):
    events: list[newtua.ProgressEvent] = []
    with Archive(HELLO_7Z) as ar:
        ar.extract(tmp_path, wrapper=False, progress=events.append)
    assert any(isinstance(e, EntryStarted) for e in events)


def test_format_and_repr():
    with Archive(HELLO_7Z) as ar:
        assert ar.format is Format.SEVENZ
        assert "7z" in repr(ar)
        assert "1 entries" in repr(ar) or "1 entry" in repr(ar)


def test_needs_password_is_false_for_a_plain_archive():
    with Archive(HELLO_7Z) as ar:
        assert ar.needs_password is False


def test_closed_archive_rejects_use():
    ar = Archive(HELLO_7Z)
    len(ar)
    ar.close()
    with pytest.raises(ValueError):
        len(ar)


def test_unknown_format_raises_typed_error(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"not an archive at all, definitely not")
    with pytest.raises(newtua.UnknownFormatError):
        len(Archive(bad))


def test_safe_mtime_and_kind_tolerate_garbage_from_untrusted_archives():
    """Old formats routinely carry junk timestamps/kinds; must not blow up
    listing the whole archive over one bad entry."""
    assert _safe_mtime(None) is None
    assert _safe_mtime(1e18) is None  # OverflowError from datetime.fromtimestamp
    assert _safe_mtime(-1e18) is None
    assert _safe_mtime(0) is not None  # a normal timestamp still works
    assert _safe_kind("file") is EntryKind.FILE
    assert _safe_kind("dir") is EntryKind.DIR
    assert _safe_kind("something the engine invents later") is EntryKind.FILE


def test_read_by_bad_index_and_name_raises_typed_errors():
    """`read()` must let `_index_of`'s own errors through, not repackage them."""
    with Archive(HELLO_7Z) as ar:
        with pytest.raises(IndexError):
            ar.read(999)
        with pytest.raises(KeyError):
            ar.read("нет-такого")


def test_accepts_a_file_object_as_a_source():
    with open(HELLO_7Z, "rb") as fh, Archive(fh) as ar:
        assert ar.read(0) == b"hello 7z"


def test_progress_returning_false_aborts_extraction(tmp_path):
    with Archive(HELLO_7Z) as ar:
        report = ar.extract(tmp_path, wrapper=False, progress=lambda event: False)
    assert report.aborted is True


def test_detected_encoding_is_reported():
    with Archive(HELLO_7Z) as ar:
        assert isinstance(ar.detected_encoding, str)
        assert ar.detected_encoding != ""


def test_bytes_source_open_failure_leaves_no_tempfile():
    before = _newtua_tempfiles()
    with pytest.raises(newtua.UnknownFormatError):
        len(Archive(b"not an archive at all, definitely not"))
    assert _newtua_tempfiles() == before


def test_tempfile_is_removed_once_unreferenced_archive_is_collected():
    before = _newtua_tempfiles()
    ar = Archive(HELLO_7Z.read_bytes())
    len(ar)  # force materialisation into a temp file plus opening it
    assert _newtua_tempfiles() - before  # the temp file now exists
    del ar
    gc.collect()  # entries hold `_archive` back-references, so it's a cycle
    assert _newtua_tempfiles() == before


def test_password_retry_does_not_leak_tempfiles():
    """Trying several passwords on a bytes-backed archive is the whole point
    of the lazy design (ask `needs_password`, set `password`, try again) —
    each retry must reuse the one spilled temp file, not spill a fresh one
    and orphan the last."""
    before = _newtua_tempfiles()
    ar = Archive(HELLO_7Z.read_bytes())
    len(ar)
    created = _newtua_tempfiles() - before
    assert len(created) == 1

    ar.password = "неверный"
    len(ar)
    assert _newtua_tempfiles() - before == created

    ar.password = "ещё один неверный"
    len(ar)
    assert _newtua_tempfiles() - before == created

    ar.close()
    assert _newtua_tempfiles() == before


# ── имя записи и угаданная кодировка на архиве не в UTF-8 ───────────────


CP1251_NAME = "привет.txt".encode("cp1251")


def cp1251_zip(path: pathlib.Path) -> pathlib.Path:
    """Собрать zip с именем в cp1251 — сырыми байтами, без флага UTF-8.

    `zipfile` сам бы перекодировал не-ASCII имя в UTF-8 и выставил флаг 0x800,
    что как раз убило бы то, ради чего фикстура нужна: подмену видно только на
    имени, которое настоящим UTF-8 не является.
    """
    import zipfile

    class RawInfo(zipfile.ZipInfo):
        def _encodeFilenameFlags(self):
            return CP1251_NAME, 0

    info = RawInfo()
    info.filename = CP1251_NAME.decode("latin-1")
    info.date_time = (2020, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(info, b"salut")
    return path


def test_raw_name_is_the_bytes_the_archive_recorded(tmp_path):
    """`raw_name` — ровно байты из архива, целые и не декодированные.

    Раньше они успевали пройти через `from_utf8_lossy` ещё в Rust, и любое
    имя не в UTF-8 доезжало сюда набором символов замены.
    """
    with Archive(cp1251_zip(tmp_path / "cp1251.zip")) as ar:
        assert ar[0].raw_name == CP1251_NAME
        assert b"\xef\xf0" in ar[0].raw_name  # не «замены», а настоящие байты


def test_detected_encoding_sees_the_real_bytes(tmp_path):
    """Кодировка угадывается по настоящим байтам всех имён.

    Раньше сюда приезжали уже испорченные имена, и вердикт всегда выходил
    «UTF-8» — то есть единственная возможность, ради которой правился движок,
    не работала вовсе.
    """
    with Archive(cp1251_zip(tmp_path / "cp1251.zip")) as ar:
        assert ar.detected_encoding == "windows-1251"
        # Вердикт согласован с тем, как движок сам расшифровал имя.
        assert ar[0].raw_name.decode(ar.detected_encoding) == "привет.txt"
        assert str(ar[0].path) == "привет.txt"


def test_encoding_override_is_honoured(tmp_path):
    """Явно заданная кодировка перебивает угадывание — и в вердикте тоже."""
    with Archive(cp1251_zip(tmp_path / "cp1251.zip"), encoding="koi8-r") as ar:
        assert ar.detected_encoding.lower() == "koi8-r"


# ── имя папки-обёртки для источников без пути ───────────────────────────


def test_wrapper_is_named_after_the_archive_for_a_path(tmp_path, two_entry_zip):
    dest = tmp_path / "out"
    with Archive(two_entry_zip) as ar:
        ar.extract(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["multi"]


def test_bytes_source_does_not_wrap_in_a_temp_file_name(tmp_path, two_entry_zip):
    """У байтов имени нет вовсе, поэтому обёртки быть не должно.

    Раньше папка называлась по случайному временному файлу — что-нибудь вроде
    `tmp3y52e4ks`: деталь реализации, вылезшая в папку пользователя.
    """
    dest = tmp_path / "out"
    with Archive(two_entry_zip.read_bytes()) as ar:
        ar.extract(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["a.txt", "b.txt"]


def test_file_object_wraps_under_the_name_it_was_opened_with(tmp_path, two_entry_zip):
    """У файлового объекта имя есть — по нему обёртку и называем."""
    dest = tmp_path / "out"
    with open(two_entry_zip, "rb") as fh, Archive(fh) as ar:
        ar.extract(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["multi"]


def test_nameless_stream_does_not_wrap(tmp_path, two_entry_zip):
    """`io.BytesIO` имени не имеет — ведём себя как с голыми байтами."""
    import io

    dest = tmp_path / "out"
    with Archive(io.BytesIO(two_entry_zip.read_bytes())) as ar:
        ar.extract(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["a.txt", "b.txt"]


def test_wrapper_false_still_wins_for_a_path_source(tmp_path, two_entry_zip):
    dest = tmp_path / "out"
    with Archive(two_entry_zip) as ar:
        ar.extract(dest, wrapper=False)
    assert sorted(p.name for p in dest.iterdir()) == ["a.txt", "b.txt"]


# ── needs_password не должен глотать чужие ошибки ───────────────────────


def test_needs_password_reports_the_real_error_for_a_non_archive(tmp_path):
    """На файле, который вовсе не архив, ответом должна быть настоящая
    причина, а не «нужен пароль».

    Раньше свойство ловило любую ошибку движка и отвечало True, пряча
    `UnknownFormatError` за приглашением ввести пароль, которого не существует.
    """
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00" * 4096)
    with pytest.raises(newtua.UnknownFormatError):
        Archive(junk).needs_password


def test_needs_password_is_true_when_the_listing_itself_is_encrypted(tmp_path):
    """Зашифрованный заголовок по-прежнему даёт True, а не пробрасывает ошибку."""
    from newtua._archive import Archive as ArchiveClass

    class HeaderEncrypted(ArchiveClass):
        def _listing(self):
            raise newtua.PasswordRequiredError("заголовок зашифрован")

    assert HeaderEncrypted(HELLO_7Z).needs_password is True


# ── адаптер записи в Rust не должен верить sink'у на слово ──────────────


def test_a_sink_overreporting_written_bytes_does_not_crash():
    """Sink, вернувший больше длины куска, раньше ронял процесс паникой:
    число уходило в срез буфера как есть."""

    class LyingSink:
        def __init__(self):
            self.total = 0

        def write(self, chunk: bytes) -> int:
            self.total += len(chunk)
            return len(chunk) * 10  # заведомо больше, чем дали

    sink = LyingSink()
    with Archive(HELLO_7Z) as ar:
        ar._open().write_entry_to(0, sink)
    assert sink.total == len(b"hello 7z")


def test_a_sink_raising_keeps_its_own_exception_class():
    """Исключение приёмника должно доехать до вызывающей стороны как есть.

    Раньше Rust превращал его в текст внутри ошибки ввода-вывода, и класс
    терялся — а разбирать текст сообщения нельзя (см. `_errors`).
    """

    class SinkFailure(RuntimeError):
        pass

    class FailingSink:
        def write(self, chunk: bytes) -> int:
            raise SinkFailure("приёмник отказался писать")

    with Archive(HELLO_7Z) as ar:
        with pytest.raises(SinkFailure, match="приёмник отказался писать"):
            ar._open().write_entry_to(0, FailingSink())


# ── повторяющиеся имена: по имени находится первая запись ───────────────


def test_lookup_by_name_finds_the_first_of_the_duplicates(tmp_path):
    """Имена в архивах повторяются; указатель «имя → запись» обязан хранить
    первую, ровно как это делал прежний перебор по порядку."""
    import io
    import tarfile

    archive = tmp_path / "dup.tar"
    with tarfile.open(archive, "w") as tf:
        for payload in (b"first", b"second"):
            info = tarfile.TarInfo("same.txt")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

    with Archive(archive) as ar:
        assert [e.index for e in ar] == [0, 1]
        assert ar["same.txt"].index == 0
        assert ar.read("same.txt") == b"first"
