# newtua (привязки для Python)

Питоновские привязки к [newtua](https://github.com/new-the-unarchiver) — быстрому распаковщику архивов на Rust (переосмысление The Unarchiver). Пакет только читает и распаковывает содержимое: он никогда не создаёт архивы. Понимает 55 форматов — от zip, 7z, rar и tar до образов дисков (DMG, ISO, WIM) и старых форматов вроде StuffIt и ARJ. Вся работа идёт внутри процесса Python, без запуска внешних программ.

## Установка

```bash
pip install newtua
```

Нужен Python 3.11 или новее. Пакет типизирован (`py.typed`) и проходит проверку `mypy --strict`, так что подсказки типов в редакторе работают сразу.

## Быстрый старт

```python
import newtua

with newtua.Archive("photos.zip") as ar:
    for entry in ar:
        print(entry.path, entry.size)
    ar.extract("out/")
```

## Открытие архива

`Archive` принимает путь к файлу, готовые байты или уже открытый двоичный файловый объект. Конструктор ничего не открывает сразу — настоящее открытие происходит при первом обращении к архиву. Это сделано специально: так можно сначала спросить, нужен ли пароль, и только потом его передать.

```python
import pathlib

import newtua

# путь на диске
with newtua.Archive("archive.7z") as ar:
    print(len(ar))

# байты — без временного файла
data = pathlib.Path("archive.7z").read_bytes()
with newtua.Archive(data) as ar:
    print(len(ar))

# уже открытый файловый объект
with open("archive.7z", "rb") as fh, newtua.Archive(fh) as ar:
    print(len(ar))
```

`Archive` работает и без `with`, но тогда временный файл (если источником были байты или поток) и открытый читатель нужно закрывать самому через `ar.close()`.

## Перебор и доступ к записям

Архив ведёт себя как обычная последовательность: длина, перебор, доступ по номеру или по имени, срез, проверка «есть ли такая запись».

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    print(len(ar))
    for entry in ar:
        print(entry.path, entry.kind, entry.size, entry.is_encrypted)
    print(ar[0])                # по номеру
    print(ar["a.txt"])           # по имени
    print("a.txt" in ar)         # True
    print(ar[0:1])               # срез — кортеж записей
```

Каждая запись — это `newtua.Entry` с полями `index`, `path`, `raw_name`, `kind`, `size`, `is_encrypted`, `mode`, `mtime` и своими методами: `entry.is_file()`, `entry.is_dir()`, `entry.is_symlink()`, `entry.read()`, `entry.open()`, `entry.extract(куда)`.

`path` — расшифрованный и приведённый в порядок путь, с ним и работают. `raw_name` — это `bytes`: имя ровно теми байтами, что записаны в архиве, без всякой расшифровки. Именно на него должны смотреть проверки безопасности пути, потому что расшифровка тут — потеря: имена в архивах не обязаны быть в UTF-8, и превращение их в строку затёрло бы как раз то, ради чего проверка затевалась. Расшифровать самому можно через `ar.detected_encoding` (см. ниже).

`entry.extract(куда)` кладёт запись прямо в указанную папку, без папки-обёртки: одна названная запись разбежаться по `куда` не может, а заворачивать её значило бы закопать на уровень глубже, чем просили. Собственный путь записи внутри архива при этом сохраняется.

Обращение по несуществующему имени поднимает `newtua.EntryNotFoundError` — это подкласс `KeyError`, так что код вокруг `ar["имя"]` ведёт себя так же, как код вокруг словаря. Номер вне диапазона даёт `IndexError`, а обращение к уже закрытому архиву — `ValueError`.

## Чтение и распаковка

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    data = ar.read(0)            # bytes целиком в память
    with ar.open("a.txt") as f:   # файловый объект newtua.EntryStream
        text = f.read()

    report = ar.extract("out/")
    print(report.extracted, report.failed, report.aborted)
```

`ar.open(...)` возвращает `newtua.EntryStream` — обычный двоичный файловый объект. Его можно передать в `shutil.copyfileobj`, скормить `json.load` или обернуть в `io.TextIOWrapper`, чтобы читать текстовую запись построчно:

```python
import io
import newtua

with newtua.Archive("archive.7z") as ar, ar.open("a.txt") as f:
    for line in io.TextIOWrapper(f, encoding="utf-8"):
        print(line)
```

Одна оговорка про типы: `EntryStream` наследует `io.BufferedIOBase`, а не протокол `typing.BinaryIO`. Это разные вещи, и функция с явной аннотацией `BinaryIO` (равно как и `IO[bytes]`) объект не примет — `mypy` пожалуется на несовместимый тип, хотя во время работы всё бы прекрасно читалось. Свои функции под этот объект аннотируйте как `io.BufferedIOBase` или прямо как `newtua.EntryStream`.

### Чтение на лету (`stream=True`)

У чтения есть два режима, и по умолчанию работает первый.

**Обычный (`stream=False`)** — запись распаковывается целиком заранее: небольшая остаётся в памяти, крупная уходит во временный файл. Результат можно перематывать (`seek`, `tell`), и это ровно то, чего ждёт большинство кода.

**На лету (`stream=True`)** — запись распаковывается рабочим потоком прямо в канал ОС, а вы читаете с другого конца. Отличия ровно три:

- **Память постоянная.** Хоть гигабайтная запись — расход не растёт вместе с ней.
- **Первый байт приходит сразу**, не дожидаясь распаковки последнего.
- **Перемотки нет.** `seekable()` даёт `False`, а `seek()` и `tell()` поднимают `io.UnsupportedOperation`.

```python
import hashlib
import newtua

with newtua.Archive("huge.zip") as ar, ar.open("big.iso", stream=True) as f:
    digest = hashlib.sha256()
    while chunk := f.read(1024 * 1024):
        digest.update(chunk)
    print(digest.hexdigest())
```

Берите этот режим, когда запись велика, а нужен только один проход по ней: посчитать хеш, перелить в сеть, разобрать потоковым парсером. Для всего остального обычный режим удобнее и остаётся умолчанием.

Читать поток можно и после того, как архив закрыт: `ar.open(..., stream=True)` не возвращает управление, пока рабочий поток не открыл архив, — так что источник у него из-под ног уже не уедет. По той же причине ошибка открытия (неверный пароль, битый заголовок) приходит исключением прямо на вызове `open`, а не оборванным на середине чтением.

Под Windows режим работает для источника-**пути**: Rust сам создаёт канал, а Python перенимает читающий конец. У источника-`bytes` или потока он поднимает `NotImplementedError` — там запись сливается во временный файл, а Windows не даёт удалить его, пока рабочий поток держит архив открытым. Берите путь или `stream=False`.

`ar.extract(куда, ...)` возвращает `newtua.Report` с числом распакованных и не распакованных записей и флагом отмены. По умолчанию (`wrapper=True`) содержимое без общего корневого каталога разворачивается в папку, названную по имени архива — так же, как это делает оригинальный The Unarchiver. Передайте `wrapper=False`, чтобы распаковать как есть. Параметр `selection` берёт список номеров, имён или самих записей и распаковывает только их.

Название обёртки берётся из имени источника. У архива, открытого из голых байтов или из `io.BytesIO`, имени нет вовсе — тогда обёртки не будет, и содержимое ляжет в `куда` как есть. У файлового объекта, открытого из файла, имя есть, и обёртка называется по нему.

## Пароль

`needs_password` подсказывает, зашифрован ли архив, — можно проверить это раньше, чем спрашивать пароль у человека. Пароль передаётся либо сразу в конструкторе, либо позже через свойство `password`: смена пароля просто открывает архив заново.

```python
import newtua

ar = newtua.Archive("secret.zip")
ar.password = "неверный"
try:
    ar.read(0)
except newtua.WrongPasswordError:
    ar.password = "правильный"
data = ar.read(0)
ar.close()
```

Отсутствие пароля там, где он нужен, поднимает `newtua.PasswordRequiredError`, неверный — `newtua.WrongPasswordError`.

## Кодировка имён

Имена внутри старых архивов не всегда в UTF-8. `Archive` сам определяет кодировку по именам файлов и умеет сообщить, что угадал:

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    print(ar.detected_encoding)   # например, "windows-1251"
```

Вердикт один на весь архив: движок решает его при открытии, разом по настоящим байтам всех имён. Поэтому им же можно расшифровать и сырое имя:

```python
import newtua

with newtua.Archive("старый.zip") as ar:
    for entry in ar:
        print(entry.raw_name, "→", entry.raw_name.decode(ar.detected_encoding))
```

Параметр `encoding=` в конструкторе переопределяет угадывание, если оно ошиблось; `detected_encoding` тогда возвращает именно заданную кодировку.

## Формат архива

```python
import newtua

with newtua.Archive("archive.7z") as ar:
    print(ar.format)              # "7z"
    print(ar.format == "7z")      # True
```

`ar.format` возвращает `newtua.Format` — это `StrEnum`, поэтому его можно сравнивать прямо со строкой.

## Прогресс

```python
import newtua

def show(event: newtua.ProgressEvent) -> None:
    match event:
        case newtua.EntryStarted(path=path, size=size):
            print(f"{path} ({size} байт)")
        case newtua.BytesWritten(written=written):
            pass  # можно рисовать прогресс-бар
        case newtua.EntryFinished():
            print("готово")

with newtua.Archive("big.zip") as ar:
    ar.extract("out/", progress=show)
```

Обработчик получает один объект события за раз — `EntryStarted`, `BytesWritten` или `EntryFinished`. Верните `False` из обработчика, чтобы отменить распаковку на середине.

## Параллелизм

Много архивов разом (эффект «распаковать пачку»):

```python
import newtua

results = newtua.extract_many(
    [("a.zip", "out/a"), ("b.7z", "out/b")],
    backend="thread",          # GIL отпускается → настоящие ядра; "process" — изоляция
    on_result=lambda job, report: print(job.archive, report.extracted),
)
```

Так же можно листать много архивов, не распаковывая:

```python
import newtua

results = newtua.list_many(["a.zip", "b.7z", "c.rar"])
for r in results:
    if r.error is None:
        print(r.archive, len(r.entries))
```

Один архив в asyncio (распаковка не морозит цикл):

```python
async with newtua.AsyncArchive("big.dmg") as ar:
    async with ar.open("payload.bin") as stream:
        async for chunk in stream:
            ...
    report = await ar.extract("out/")
```

Оговорки: колбэк `progress` вызывается из рабочего потока (в цикл — только через
`loop.call_soon_threadsafe`); `backend="process"` не поддерживает `progress`; на
Windows потоковый режим требует путь-источник (не `bytes`/поток).

## Ошибки

Все ошибки движка наследуют `newtua.NewtuaError`:

- `UnknownFormatError` — формат архива не распознан.
- `UnsupportedError` — формат известен, но именно эта возможность не поддерживается.
- `PasswordRequiredError` — архив зашифрован, пароль не передан.
- `WrongPasswordError` — переданный пароль не подошёл.
- `CorruptArchiveError` — данные архива повреждены.
- `MissingVolumeError` — не хватает тома в многотомном архиве.
- `UnsafePathError` — путь записи попытался бы выйти за пределы папки распаковки.

Отдельно стоит `newtua.EntryNotFoundError` — он наследует `KeyError`, а не `NewtuaError`, потому что обращение к записи по имени должно вести себя как обращение к словарю. Обращение по номеру вне диапазона даёт обычный `IndexError`, а любая операция с уже закрытым архивом — `ValueError`.

## Как тестировать

Архив можно открыть прямо из байтов, поэтому временный файл в тесте не нужен, а обработчик прогресса — это обычный список, без всякой обёртки:

```python
import pathlib
import newtua
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

def test_extracts_everything(tmp_path):
    data = (FIXTURES / "hello.7z").read_bytes()
    with newtua.Archive(data) as ar:
        report = ar.extract(tmp_path, wrapper=False)
    assert report.extracted == 1
    assert (tmp_path / "a.txt").read_text() == "hello 7z"

def test_collects_progress(tmp_path):
    events: list[newtua.ProgressEvent] = []
    with newtua.Archive(FIXTURES / "hello.7z") as ar:
        ar.extract(tmp_path, progress=events.append)
    assert any(isinstance(e, newtua.EntryStarted) for e in events)

def test_unknown_format(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"это точно не архив")
    with pytest.raises(newtua.UnknownFormatError):
        len(newtua.Archive(bad))
```

Фикстура `tmp_path` — обычная фикстура pytest, отдельная временная папка на каждый тест. Исключения проверяются точным типом через `pytest.raises`, а не разбором текста сообщения.

## Разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install maturin pytest
cd crates/newtua-py
maturin develop                          # собрать и установить в venv
python -m pytest tests
```

### Сборка колёс

`abi3` (стабильный ABI, начиная с Python 3.11) — одно колесо на платформу покрывает все Python ≥ 3.11. Подсказки типов едут внутри: сам пакет типизирован в коде и помечен `py.typed`, а заглушка `_newtua.pyi` описывает скомпилированную часть.

```bash
maturin build --release
```

**Релизная сборка (в CI):** колёса для Windows x86_64, macOS arm64 + x86_64 и Linux manylinux x86_64 + aarch64 (musllinux — опционально), через `maturin-action`/cibuildwheel в GitHub Actions. Зависимости с C-кодом (bzip2, xz, AES для 7z, вендоренный libunrar) собираются из исходников, поэтому сборочным образам нужен C-тулчейн (особенно для кросс-сборки под aarch64).

> На самом новом CPython (например, 3.14) сборка abi3-расширения может потребовать `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`, пока PyO3 явно не перечислит этот интерпретатор в поддерживаемых.
