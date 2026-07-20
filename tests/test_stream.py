import io
import json
import os
import pathlib
import resource
import shutil
import sys
import time
import zipfile

import newtua
from newtua._stream import EntryStream, _PipeStream

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def writer_of(payload: bytes):
    def write_into(sink) -> None:
        for i in range(0, len(payload), 4096):
            sink.write(payload[i : i + 4096])

    return write_into


def test_reads_like_a_file():
    with EntryStream.from_writer(writer_of(b"hello 7z")) as f:
        assert f.read() == b"hello 7z"


def test_is_a_real_binary_io():
    with EntryStream.from_writer(writer_of(b"abc")) as f:
        assert f.readable() and f.seekable()
        assert f.read(1) == b"a"
        f.seek(0)
        assert f.read() == b"abc"


def test_works_with_the_stdlib():
    payload = json.dumps({"hello": "мир"}).encode()
    with EntryStream.from_writer(writer_of(payload)) as f:
        assert json.load(f)["hello"] == "мир"

    with EntryStream.from_writer(writer_of(b"x" * 100_000)) as f, io.BytesIO() as out:
        shutil.copyfileobj(f, out)
        assert out.tell() == 100_000


def test_large_payload_spills_to_disk():
    big = b"x" * (2 * 1024 * 1024)
    with EntryStream.from_writer(writer_of(big), max_in_memory=1024) as f:
        assert f.read() == big


def test_closed_stream_rejects_reads():
    f = EntryStream.from_writer(writer_of(b"abc"))
    f.close()
    try:
        f.read()
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("чтение закрытого объекта должно давать ValueError")


def test_read1_allows_text_io_wrapper_line_reading():
    payload = "первая строка\nвторая строка\n".encode("utf-8")
    with EntryStream.from_writer(writer_of(payload)) as f:
        text = io.TextIOWrapper(f, encoding="utf-8")
        assert text.readline() == "первая строка\n"
        assert text.readline() == "вторая строка\n"


def test_readinto_fills_preallocated_buffer():
    with EntryStream.from_writer(writer_of(b"hello 7z")) as f:
        buffer = bytearray(5)
        n = f.readinto(buffer)
        assert n == 5
        assert bytes(buffer) == b"hello"


def test_stream_is_read_only():
    with EntryStream.from_writer(writer_of(b"abc")) as f:
        assert f.writable() is False
        try:
            f.write(b"x")
        except io.UnsupportedOperation:
            pass
        else:  # pragma: no cover
            raise AssertionError("запись в поток только для чтения должна давать UnsupportedOperation")


# ── stream=True: распаковка в канал ОС рабочим потоком ──────────────────


CHUNK = b"\xa5" * 65536


def zip_of_repeated_chunks(path, chunks: int):
    """Собрать zip с одной большой записью, не держа её целиком в памяти."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("payload.bin", "w") as sink:
            for _ in range(chunks):
                sink.write(CHUNK)
    return path


def fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def test_stream_mode_reads_the_same_bytes():
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        with ar.open(0, stream=True) as f:
            assert f.read() == b"hello 7z"


def test_stream_is_not_seekable():
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        with ar.open(0, stream=True) as f:
            assert f.seekable() is False
            assert f.readable() is True
            try:
                f.seek(0)
            except io.UnsupportedOperation:
                pass
            else:  # pragma: no cover
                raise AssertionError("перемотка стрима должна давать UnsupportedOperation")


def test_stream_matches_buffered_mode_byte_for_byte(tmp_path):
    archive = zip_of_repeated_chunks(tmp_path / "payload.zip", 24)
    with newtua.Archive(archive) as ar:
        with ar.open("payload.bin") as buffered:
            expected = buffered.read()
        with ar.open("payload.bin", stream=True) as streamed:
            got = streamed.read()
    assert got == expected == CHUNK * 24


def test_stream_reads_in_pieces(tmp_path):
    archive = zip_of_repeated_chunks(tmp_path / "payload.zip", 8)
    with newtua.Archive(archive) as ar, ar.open(0, stream=True) as f:
        pieces = []
        while True:
            piece = f.read(4096)
            if not piece:
                break
            pieces.append(piece)
    assert b"".join(pieces) == CHUNK * 8


def test_closing_mid_read_leaks_no_descriptors(tmp_path):
    """Уход читателя на середине: процесс жив, рабочий поток доигрывает и уходит.

    За уходом потока следим по дескрипторам: он держит пишущий конец канала и
    заново открытый архив, и оба освобождаются только при его завершении.
    Питоновский `threading.active_count` тут бесполезен — потоков, заведённых
    Rust, он не видит вовсе.
    """
    archive = zip_of_repeated_chunks(tmp_path / "big.zip", 1024)  # 64 MiB

    with newtua.Archive(archive) as ar:
        # Счёт берётся с уже открытым архивом: его собственный дескриптор к
        # стриму отношения не имеет.
        fds_before = fd_count()
        f = ar.open(0, stream=True)
        assert len(f.read(4096)) == 4096
        assert fd_count() > fds_before  # канал и правда открыт
        f.close()

        # Рабочий поток узнаёт об уходе читателя только на следующей записи,
        # поэтому даём ему мгновение доиграть.
        for _ in range(100):
            if fd_count() == fds_before:
                break
            time.sleep(0.02)
        assert fd_count() == fds_before


def test_stream_memory_stays_flat(tmp_path):
    archive = zip_of_repeated_chunks(tmp_path / "big.zip", 1024)  # 64 MiB
    before = maxrss_bytes()
    with newtua.Archive(archive) as ar, ar.open(0, stream=True) as f:
        total = 0
        while True:
            piece = f.read(65536)
            if not piece:
                break
            total += len(piece)
    assert total == 64 * 1024 * 1024
    # Постоянная память: рост много меньше самой записи.
    assert maxrss_bytes() - before < 16 * 1024 * 1024


def maxrss_bytes() -> int:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def test_bad_index_raises_instead_of_streaming_nothing():
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        try:
            ar.open(99, stream=True)
        except IndexError:
            pass
        else:  # pragma: no cover
            raise AssertionError("неверный номер записи должен давать IndexError")


def test_engine_rejects_a_bad_index_before_taking_the_pipe():
    """Сторож в Rust: номер проверяется до того, как поток начнёт писать."""
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        reader = ar._open()
        read_fd, write_fd = os.pipe()
        try:
            reader.open_stream(99, write_fd)
        except Exception:
            pass
        else:  # pragma: no cover
            raise AssertionError("движок должен отказать на неверном номере")
        finally:
            os.close(read_fd)
            os.close(write_fd)


# ── stream=True: сверка длины против Entry.size ─────────────────────────


def test_stream_mode_full_read_matches_buffered_mode_when_intact():
    """Целая запись в режиме stream=True не бросает исключений и совпадает
    с обычным режимом побайтово — сверка длины не мешает штатному чтению."""
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        with ar.open(0, stream=True) as streamed:
            got = streamed.read()
        with ar.open(0) as buffered:
            expected = buffered.read()
    assert got == expected == b"hello 7z"


def test_pipe_stream_raises_when_data_ends_short_of_expected_size():
    """Логика сверки напрямую: бэкинг отдаёт меньше, чем обещал `expected_size`.

    Настоящую повреждённую фикстуру для этого не нужна и её нет в наборе —
    моделируем обрыв, заведомо завысив ожидаемый размер над тем, что реально
    лежит в канале.
    """
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as w:
        w.write(b"short")  # пишущий конец закрывается сразу после — это EOF
    f = _PipeStream(os.fdopen(read_fd, "rb", buffering=0), expected_size=100)
    try:
        f.read()
    except newtua.CorruptArchiveError as exc:
        message = str(exc)
        assert "5" in message and "100" in message
    else:  # pragma: no cover
        raise AssertionError("обрыв данных должен давать CorruptArchiveError")


def test_pipe_stream_allows_closing_before_expected_size_is_reached():
    """Уход читателя на середине — не ошибка: сверка молчит, если до конца
    данных читатель не дошёл (см. test_closing_mid_read_leaks_no_descriptors
    для сценария через полноценный архив)."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as w:
        w.write(b"0123456789")
    f = _PipeStream(os.fdopen(read_fd, "rb", buffering=0), expected_size=100)
    assert f.read(5) == b"01234"
    f.close()  # не читаем до конца — молчаливый уход, не CorruptArchiveError


# ── stream=True: рукопожатие с рабочим потоком ──────────────────────────


def test_stream_survives_the_archive_closing_right_after_it_was_opened():
    """Гонка со сносом временного файла.

    Источник — байты, поэтому архив разложен во временный файл. Стрим берётся
    внутри `with`, а читается уже снаружи: к моменту чтения `close()` временный
    файл удалил.

    Лечилось в два приёма. Сначала рабочий поток открывал архив уже после
    возврата из `open_stream` и в большинстве прогонов не заставал файла на
    месте (184 падения из 200) — это закрыло рукопожатие. Осталось окно поуже:
    для 7z и RAR движок переоткрывает архив по пути прямо в `read_entry`, то
    есть уже после рукопожатия, и редкие падения продолжались. Закрыто тем, что
    временный файл живёт, пока жив хоть один стрим.

    Нагрузочный тест: сама гонка ловится только случаем. Свойство, которым она
    закрыта, проверяется отдельно и без гонки — см.
    `test_tempfile_outlives_the_archive_while_a_stream_is_still_open`.
    """
    data = (FIXTURES / "hello.7z").read_bytes()
    failures = []
    for _ in range(200):
        try:
            with newtua.Archive(data) as ar:
                f = ar.open(0, stream=True)
            with f:
                got = f.read()
            if got != b"hello 7z":
                failures.append(got)
        except Exception as exc:  # считаем любые падения, какими бы ни были
            failures.append(exc)
    assert failures == []


def test_tempfile_outlives_the_archive_while_a_stream_is_still_open():
    """Свойство, которым закрыта гонка, — проверяемое без гонки.

    Само падение ловится только случаем: окно между возвратом из `open_stream`
    и переоткрытием архива по пути внутри `read_entry` — микросекунды. Поэтому
    проверяем не исход гонки, а условие, при котором её нет вовсе: пока стрим
    жив, временный файл на месте, хотя архив уже закрыт.
    """
    data = (FIXTURES / "hello.7z").read_bytes()
    ar = newtua.Archive(data)
    f = ar.open(0, stream=True)
    tempfile = ar._tempfile
    assert tempfile is not None
    ar.close()
    assert tempfile.path.exists(), "архив закрыт, но стрим ещё читает файл"
    assert f.read() == b"hello 7z"
    f.close()
    assert not tempfile.path.exists(), "последний держатель ушёл, а файл остался"


def test_stream_that_fails_to_open_does_not_pin_the_tempfile(tmp_path):
    """Провал открытия снимает заявку — иначе файл жил бы до сборки мусора."""
    archive = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", b"payload")
    ar = newtua.Archive(archive.read_bytes())
    assert len(ar) == 1
    tempfile = ar._tempfile
    assert tempfile is not None
    tempfile.path.write_bytes(b"\x00" * 64)  # рабочий поток уже не откроет
    try:
        ar.open(0, stream=True)
    except newtua.NewtuaError:
        pass
    else:  # pragma: no cover
        raise AssertionError("ошибка открытия должна подниматься исключением")
    ar.close()
    assert not tempfile.path.exists(), "заявка провалившегося стрима не снята"


def test_open_failure_raises_at_the_call_instead_of_emptying_the_pipe(tmp_path):
    """Ошибка открытия приходит исключением в момент вызова.

    Побочная выгода рукопожатия: раньше неверный пароль оборачивался молча
    оборванным каналом, и узнать о нём можно было только по недобору байт.
    """
    archive = tmp_path / "secret.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", b"secret")
    # Ломаем архив после того, как он открылся и записи прочитаны: рабочий
    # поток открывает его заново и уже не сможет.
    with newtua.Archive(archive) as ar:
        assert len(ar) == 1
        archive.write_bytes(b"\x00" * 64)
        try:
            ar.open(0, stream=True)
        except newtua.NewtuaError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ошибка открытия должна подниматься исключением")


def test_failed_open_leaves_no_descriptors_behind(tmp_path):
    """Провал открытия не должен терять ни одного конца канала."""
    archive = tmp_path / "secret.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", b"secret")
    with newtua.Archive(archive) as ar:
        assert len(ar) == 1
        archive.write_bytes(b"\x00" * 64)
        before = fd_count()
        for _ in range(20):
            try:
                ar.open(0, stream=True)
            except newtua.NewtuaError:
                pass
        assert fd_count() == before


# ── stream=True: read(n) отдаёт ровно n ─────────────────────────────────


def test_read_returns_exactly_what_was_asked_for(tmp_path):
    """`io.BufferedIOBase` обещает ровно `n` байт, пока данные не кончились.

    Раньше дескриптор канала заворачивался с `buffering=0`, поэтому `read(n)`
    сводился к одному системному вызову: запрос 1 МиБ возвращал 8192 байта при
    8 МиБ впереди, и код вида `struct.unpack(f.read(4))` молча получал меньше.
    Цикл «читать до пустоты» этого не замечает — здесь спрашиваем разом больше,
    чем отдаёт одно чтение канала.
    """
    archive = zip_of_repeated_chunks(tmp_path / "big.zip", 128)  # 8 МиБ
    with newtua.Archive(archive) as ar, ar.open(0, stream=True) as f:
        assert len(f.read(1024 * 1024)) == 1024 * 1024
        assert len(f.read(3 * 1024 * 1024)) == 3 * 1024 * 1024


def test_read_returns_short_only_at_the_very_end(tmp_path):
    """Недобор допустим ровно один раз — на исчерпании данных."""
    archive = zip_of_repeated_chunks(tmp_path / "big.zip", 8)  # 512 КиБ
    total = 8 * len(CHUNK)
    with newtua.Archive(archive) as ar, ar.open(0, stream=True) as f:
        first = f.read(total - 100)
        assert len(first) == total - 100
        rest = f.read(1024 * 1024)  # просим много, осталось 100
        assert len(rest) == 100
        assert f.read(16) == b""
