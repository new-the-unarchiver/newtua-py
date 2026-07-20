//! newtua-py — idiomatic Python bindings for `newtua-core` via PyO3.
//!
//! The compiled module is `newtua._newtua`; the `newtua` package (python/)
//! re-exports it. Logic lives in `newtua-core`; this crate only marshals types.

use std::path::PathBuf;
use std::time::UNIX_EPOCH;

use newtua_core::{
    ArchiveReader, Entry as CoreEntry, EntryKind, Error, ExtractOptions, Flow, FormatId,
    OpenOptions, ProgressEvent, open as core_open,
};
use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

create_exception!(
    _newtua,
    NewtuaError,
    PyException,
    "Error from the newtua engine."
);

/// Разновидность ошибки для питоновского слоя: он поднимает по ней свой класс
/// из иерархии. Разбирать текст сообщения нельзя, поэтому признак — отдельно.
fn error_kind(e: &Error) -> &'static str {
    match e {
        Error::UnknownFormat => "unknown_format",
        Error::Unsupported { .. } => "unsupported",
        Error::Encrypted => "encrypted",
        Error::WrongPassword => "wrong_password",
        Error::Corrupt(_) => "corrupt",
        Error::MissingVolume(_) => "missing_volume",
        Error::PathTraversal(_) => "path_traversal",
        Error::Io(_) => "io",
        Error::InvalidIndex(_) => "invalid_index",
    }
}

/// Собрать питоновскую ошибку из уже разобранных частей. Отдельно от
/// `to_pyerr`, потому что рабочий поток стрима не может отдать сам `Error`
/// (тот не `Send`) — он присылает текст и признак.
fn pyerr_from_parts(message: &str, kind: &'static str) -> PyErr {
    let err = NewtuaError::new_err(message.to_owned());
    Python::attach(|py| {
        // `kind` читает питоновский слой, чтобы поднять свой класс исключения;
        // текст сообщения остаётся человеку. Неудача setattr не должна
        // подменять исходную ошибку — она важнее.
        let _ = err.value(py).setattr("kind", kind);
    });
    err
}

fn to_pyerr(e: &Error) -> PyErr {
    pyerr_from_parts(&e.to_string(), error_kind(e))
}

/// Все форматы движка одной таблицей: пара «вариант ядра → имя для Python».
///
/// Отсюда растут оба потребителя — `format_name` и `ALL_FORMATS`, — поэтому
/// разойтись им негде. `match` внутри макроса намеренно без `_ =>`: добавят
/// вариант в `FormatId` — код перестанет собираться ровно здесь, и это
/// единственное место, которое надо будет поправить.
macro_rules! formats {
    ($($variant:ident => $name:literal,)+) => {
        /// Человекочитаемое имя формата.
        fn format_name(f: FormatId) -> &'static str {
            match f {
                $(FormatId::$variant => $name,)+
            }
        }

        /// Все имена форматов — для сторожа, который сверяет питоновский `Format`.
        const ALL_FORMATS: &[&str] = &[$($name,)+];
    };
}

formats! {
    Zip => "zip",
    Tar => "tar",
    Gzip => "gzip",
    Bzip2 => "bzip2",
    Xz => "xz",
    SevenZ => "7z",
    Rar => "rar",
    Cab => "cab",
    Ar => "ar",
    Deb => "deb",
    Cpio => "cpio",
    Rpm => "rpm",
    Xar => "xar",
    Msi => "msi",
    Iso => "iso",
    Sfx => "sfx",
    Warc => "warc",
    Raw => "raw",
    Jar => "jar",
    Apk => "apk",
    Ipa => "ipa",
    Epub => "epub",
    Docx => "docx",
    Xlsx => "xlsx",
    Pptx => "pptx",
    Odt => "odt",
    Ods => "ods",
    Odp => "odp",
    Crx => "crx",
    Conda => "conda",
    Squashfs => "squashfs",
    AppImage => "appimage",
    Wim => "wim",
    HfsPlus => "hfsplus",
    Dmg => "dmg",
    Apfs => "apfs",
    Arj => "arj",
    Zoo => "zoo",
    Lbr => "lbr",
    Crunch => "crunch",
    Arc => "arc",
    Squeeze => "squeeze",
    BinHex => "binhex",
    MacBinary => "macbinary",
    AppleSingle => "applesingle",
    CompactPro => "compactpro",
    PackIt => "packit",
    StuffIt => "stuffit",
    StuffIt5 => "stuffit5",
    StuffItX => "stuffitx",
    Alz => "alz",
    Nsis => "nsis",
    Lzx => "lzx",
    PowerPacker => "powerpacker",
    Dms => "dms",
}

/// Plain owned copy of an entry's metadata (the listing snapshot).
#[derive(Clone)]
struct EntryData {
    path: String,
    raw_name: Vec<u8>,
    kind: &'static str,
    size: u64,
    is_encrypted: bool,
    mode: Option<u32>,
    mtime: Option<f64>,
}

fn entry_data(e: &CoreEntry) -> EntryData {
    let kind = match e.kind {
        EntryKind::File => "file",
        EntryKind::Dir => "dir",
        EntryKind::Symlink { .. } => "symlink",
    };
    let mtime = e
        .modified
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64());
    EntryData {
        path: e.path.to_string_lossy().into_owned(),
        raw_name: e.path_raw.clone(),
        kind,
        size: e.size,
        is_encrypted: e.is_encrypted,
        mode: e.mode,
        mtime,
    }
}

/// One archive entry's metadata.
#[pyclass(name = "Entry", get_all, frozen)]
struct PyEntry {
    /// Decoded path within the archive.
    path: String,
    /// Имя записи ровно теми байтами, что записаны в архиве. Именно `bytes`,
    /// а не `str`: любое декодирование здесь — потеря, а на это поле смотрят
    /// проверки безопасности пути.
    raw_name: Py<PyBytes>,
    /// `"file"`, `"dir"`, or `"symlink"`.
    kind: String,
    /// Uncompressed size in bytes.
    size: u64,
    /// Whether this entry is encrypted.
    is_encrypted: bool,
    /// Unix permission bits, if the archive recorded them.
    mode: Option<u32>,
    /// Modification time as a Unix timestamp (seconds), if recorded.
    mtime: Option<f64>,
}

impl PyEntry {
    fn from_data(py: Python<'_>, d: &EntryData) -> PyEntry {
        PyEntry {
            path: d.path.clone(),
            raw_name: PyBytes::new(py, &d.raw_name).unbind(),
            kind: d.kind.to_owned(),
            size: d.size,
            is_encrypted: d.is_encrypted,
            mode: d.mode,
            mtime: d.mtime,
        }
    }
}

/// Result of an extraction.
#[pyclass(name = "Report", get_all, frozen)]
struct PyReport {
    extracted: usize,
    failed: usize,
    aborted: bool,
}

use std::io::Write;

/// Адаптер: `std::io::Write` поверх питоновского объекта с методом `write`.
/// Движок пишет кусками, каждый кусок уезжает в Python как `bytes`.
struct PySink {
    obj: Py<PyAny>,
    /// Исключение, которое поднял сам питоновский приёмник. `std::io::Error`
    /// довёз бы до вызывающей стороны только текст, а класс исключения —
    /// единственное, по чему её коду позволено разбирать ошибку. Поэтому
    /// исходный `PyErr` откладывается сюда и восстанавливается на возврате.
    err: Option<PyErr>,
}

impl Write for PySink {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        Python::attach(|py| {
            let chunk = PyBytes::new(py, buf);
            let ret = match self.obj.call_method1(py, "write", (chunk,)) {
                Ok(ret) => ret,
                Err(e) => {
                    let io_err = std::io::Error::other(e.to_string());
                    self.err = Some(e);
                    return Err(io_err);
                }
            };
            // `write` must report how many bytes the sink actually accepted,
            // since callers like `write_all`/`io::copy` use it to know how
            // much of `buf` still needs writing. Python's `write` contract
            // returns that count as an int; `None` (some file-like objects'
            // convention) means "all of it"; anything else gives us nothing
            // to go on, so we also assume the whole chunk was accepted.
            //
            // The count is clamped to the chunk: a sink claiming it wrote more
            // than it was given is nonsense, and an unclamped number would be
            // used to slice `buf` past its end — a panic across the FFI border.
            match ret.extract::<usize>(py) {
                Ok(n) => Ok(n.min(buf.len())),
                Err(_) => Ok(buf.len()),
            }
        })
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// An open archive. Iterate it for entries, or call `extract`/`read`.
#[pyclass(unsendable)]
struct Archive {
    reader: Box<dyn ArchiveReader>,
    entries: Vec<EntryData>,
    path: PathBuf,
    // Считается один раз при открытии, по настоящим байтам всех имён. Питон
    // сам это посчитать не может: к нему имена приезжают уже поштучно.
    detected_encoding: String,
    // Как архив был открыт. Читатель непереносим между потоками, поэтому
    // рабочий поток открывает архив заново — по этим самым значениям.
    password: Option<String>,
    encoding: Option<String>,
}

#[pymethods]
impl Archive {
    /// Container format of this archive, e.g. `"zip"`, `"7z"`.
    fn format(&self) -> &'static str {
        format_name(self.reader.format())
    }

    /// List all entries.
    fn entries(&self, py: Python<'_>) -> Vec<PyEntry> {
        self.entries
            .iter()
            .map(|d| PyEntry::from_data(py, d))
            .collect()
    }

    /// Charset label the engine picked for this archive's entry names.
    fn detected_encoding(&self) -> &str {
        &self.detected_encoding
    }

    /// Read one entry's bytes by index.
    fn read<'py>(&mut self, py: Python<'py>, index: usize) -> PyResult<Bound<'py, PyBytes>> {
        let mut buf: Vec<u8> = Vec::new();
        self.reader
            .read_entry(index, &mut buf)
            .map_err(|e| to_pyerr(&e))?;
        Ok(PyBytes::new(py, &buf))
    }

    /// Write one entry's bytes into a Python object with a `write` method.
    ///
    /// Streams in chunks: nothing accumulates in memory on the Rust side.
    ///
    /// An exception raised by the sink itself comes back out as it went in:
    /// the engine can only carry it across as an I/O error's message, so the
    /// original is kept aside and restored here.
    fn write_entry_to(&mut self, index: usize, sink: Py<PyAny>) -> PyResult<()> {
        let mut sink = PySink {
            obj: sink,
            err: None,
        };
        match self.reader.read_entry(index, &mut sink) {
            Ok(()) => Ok(()),
            Err(e) => Err(sink.err.take().unwrap_or_else(|| to_pyerr(&e))),
        }
    }

    /// Decode one entry into `write_fd` on a worker thread.
    ///
    /// The reader is created *inside* the thread and never crosses the
    /// boundary — that is what makes this legal without `Send` on
    /// `ArchiveReader`. The pipe itself is made by the caller; `write_fd` is
    /// handed over for good, and closing it is what tells the reading end that
    /// the data ended.
    ///
    /// The index is checked here, synchronously, so the common mistake still
    /// raises instead of arriving as a silently empty stream. Unix only —
    /// `os.pipe()` on Windows yields a CRT descriptor that `File::from_raw_fd`
    /// does not accept.
    ///
    /// **Рукопожатие.** Метод не возвращает управление, пока рабочий поток не
    /// открыл архив. Это закрывает гонку: источником может быть временный
    /// файл, который вызывающая сторона удалит сразу после возврата, — а к
    /// этому моменту он уже открыт (на Unix удаление открытого файла безвредно).
    /// Побочная выгода: ошибка открытия (неверный пароль, битый заголовок)
    /// поднимается исключением прямо здесь, а не оборачивается молча
    /// оборванным каналом.
    ///
    /// **Владение `write_fd`.** Дескриптор переходит к Rust только на удачном
    /// пути. Любая ошибка этого метода оставляет его нетронутым — закрывает
    /// его тогда вызывающая сторона, вместе с читающим концом.
    #[cfg(unix)]
    fn open_stream(&self, py: Python<'_>, index: usize, write_fd: i32) -> PyResult<()> {
        use std::fs::File;
        use std::os::fd::FromRawFd;
        use std::sync::mpsc::sync_channel;

        if index >= self.entries.len() {
            return Err(to_pyerr(&Error::InvalidIndex(index)));
        }
        let path = self.path.clone();
        let password = self.password.clone();
        let encoding = self.encoding.clone();

        // Ошибку отдаём разобранной на части: сам `Error` не `Send`.
        let (tx, rx) = sync_channel::<Result<(), (String, &'static str)>>(1);

        // GIL отпускается на время распаковки — это и есть задел под AsyncArchive.
        let handshake = py.detach(move || {
            std::thread::spawn(move || {
                let opts = OpenOptions {
                    password,
                    encoding_override: encoding,
                };
                let mut reader = match core_open(&path, &opts) {
                    Ok(reader) => reader,
                    Err(e) => {
                        // Дескриптор не тронут: закроет его питоновский слой.
                        let _ = tx.send(Err((e.to_string(), error_kind(&e))));
                        return;
                    }
                };
                // С этого мига дескриптор наш. Порядок важен: сначала отпускаем
                // вызывающую сторону, и только потом берём владение, — иначе
                // ранняя ошибка и удачный путь спорили бы за то, кто закрывает.
                let _ = tx.send(Ok(()));
                // SAFETY: дескриптор отдан нам во владение вызывающей стороной;
                // File закроет его при выходе, что и сигналит читателю конец.
                let mut sink = unsafe { File::from_raw_fd(write_fd) };
                // Об ошибке в середине распаковки сообщаем обрывом канала:
                // читатель увидит короткий поток и сверит его с `Entry.size`.
                // Ошибка записи — это обычно закрытый читающий конец (читатель
                // ушёл раньше времени), и она тоже просто завершает поток.
                let _ = reader.read_entry(index, &mut sink);
            });
            rx.recv()
        });

        match handshake {
            Ok(Ok(())) => Ok(()),
            Ok(Err((message, kind))) => Err(pyerr_from_parts(&message, kind)),
            // Канал оборвался, не сказав ни слова: рабочий поток запаниковал.
            // Дескриптор при этом остался нетронутым.
            Err(_) => Err(pyerr_from_parts(
                "рабочий поток распаковки завершился аварийно",
                "io",
            )),
        }
    }

    /// Extract entries to `dest`.
    ///
    /// `selection`: indices to extract (None = all). `progress`: optional
    /// callable `(event, index, path, bytes, size)`; return False to cancel.
    ///
    /// `name_source`: path whose name the wrapper folder takes; `None` means
    /// there is no such name and no wrapper folder. Никакой замены по
    /// умолчанию здесь нет намеренно: собственный путь архива — это временный
    /// файл всякий раз, когда источником были байты или поток, а знает об
    /// этом только питоновский слой. Значит и решение целиком его.
    #[pyo3(signature = (dest, selection=None, wrapper=true, strict=false, preserve=true, progress=None, name_source=None))]
    #[allow(clippy::too_many_arguments)]
    fn extract(
        &mut self,
        dest: PathBuf,
        selection: Option<Vec<usize>>,
        wrapper: bool,
        strict: bool,
        preserve: bool,
        progress: Option<Py<PyAny>>,
        name_source: Option<PathBuf>,
    ) -> PyResult<PyReport> {
        let wrapper_name = name_source
            .as_deref()
            .and_then(|p| newtua_core::wrapper_name(p, wrapper));

        let progress_fn = progress.map(|cb| {
            let boxed: newtua_core::ProgressFn = Box::new(move |ev: ProgressEvent| -> Flow {
                let (event, index, path, bytes, size) = match ev {
                    ProgressEvent::EntryStart { index, path, size } => {
                        ("start", index, Some(path.to_owned()), 0u64, size)
                    }
                    ProgressEvent::Bytes { index, written } => ("bytes", index, None, written, 0),
                    ProgressEvent::EntryDone { index } => ("done", index, None, 0, 0),
                };
                Python::attach(|py| {
                    match cb.call1(py, (event, index, path, bytes, size)) {
                        // Returning False (and only False) cancels.
                        Ok(ret) => match ret.extract::<bool>(py) {
                            Ok(false) => Flow::Abort,
                            _ => Flow::Continue,
                        },
                        Err(_) => Flow::Abort,
                    }
                })
            });
            boxed
        });

        let mut opts = ExtractOptions {
            dest,
            wrapper_name,
            strict,
            preserve,
            selection,
            progress: progress_fn,
            keep_macos_metadata: false,
        };
        // Extraction runs with the GIL held (the reader is not Send, so we can't
        // release it); the progress callback re-enters Python via Python::attach.
        let report =
            newtua_core::extract_all(&mut *self.reader, &mut opts).map_err(|e| to_pyerr(&e))?;
        Ok(PyReport {
            extracted: report.extracted,
            failed: report.failed.len(),
            aborted: report.aborted,
        })
    }
}

/// Open an archive for listing and extraction.
#[pyfunction]
#[pyo3(signature = (path, password=None, encoding=None))]
fn open(path: PathBuf, password: Option<String>, encoding: Option<String>) -> PyResult<Archive> {
    let opts = OpenOptions {
        password: password.clone(),
        encoding_override: encoding.clone(),
    };
    let mut reader = core_open(&path, &opts).map_err(|e| to_pyerr(&e))?;
    let entries: Vec<EntryData> = reader
        .entries()
        .map_err(|e| to_pyerr(&e))?
        .iter()
        .map(entry_data)
        .collect();
    // Кодировку определяем здесь, на настоящих байтах и сразу по всем именам:
    // один общий вердикт на архив, ровно как это делает само ядро.
    let raw_names: Vec<Vec<u8>> = entries.iter().map(|e| e.raw_name.clone()).collect();
    let detected_encoding = newtua_core::detect_encoding(&raw_names, encoding.as_deref());
    Ok(Archive {
        reader,
        entries,
        path,
        detected_encoding,
        password,
        encoding,
    })
}

/// Every format name the engine can report. Used by the Python-side guard.
#[pyfunction]
#[pyo3(name = "_all_formats")]
fn all_formats() -> Vec<&'static str> {
    ALL_FORMATS.to_vec()
}

#[pymodule]
fn _newtua(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Archive>()?;
    m.add_class::<PyEntry>()?;
    m.add_class::<PyReport>()?;
    m.add_function(wrap_pyfunction!(open, m)?)?;
    m.add_function(wrap_pyfunction!(all_formats, m)?)?;
    m.add("NewtuaError", m.py().get_type::<NewtuaError>())?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
