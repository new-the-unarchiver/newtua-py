"""newtua — a fast, in-process archive extractor.

```python
import newtua

with newtua.Archive("photos.zip") as ar:
    for entry in ar:
        print(entry.path, entry.size)
    ar.extract("out/")
```
"""

from ._newtua import __version__
from ._archive import Archive, Report
from ._async import AsyncArchive, AsyncEntryStream
from ._batch import (
    Backend,
    BatchResult,
    ExtractJob,
    ListingResult,
    extract_many,
    list_many,
)
from ._entry import Entry, EntryKind
from ._errors import (
    CorruptArchiveError,
    EntryNotFoundError,
    MissingVolumeError,
    NewtuaError,
    PasswordRequiredError,
    UnknownFormatError,
    UnsafePathError,
    UnsupportedError,
    WrongPasswordError,
)
from ._events import (
    BytesWritten,
    EntryFinished,
    EntryStarted,
    EventKind,
    ProgressEvent,
)
from ._format import Format
from ._stream import EntryStream

__all__ = [
    "Archive",
    "AsyncArchive",
    "AsyncEntryStream",
    "Backend",
    "BatchResult",
    "BytesWritten",
    "CorruptArchiveError",
    "Entry",
    "EntryFinished",
    "EntryKind",
    "EntryNotFoundError",
    "EntryStarted",
    "EntryStream",
    "EventKind",
    "ExtractJob",
    "Format",
    "ListingResult",
    "MissingVolumeError",
    "NewtuaError",
    "PasswordRequiredError",
    "ProgressEvent",
    "Report",
    "UnknownFormatError",
    "UnsafePathError",
    "UnsupportedError",
    "WrongPasswordError",
    "__version__",
    "extract_many",
    "list_many",
]
