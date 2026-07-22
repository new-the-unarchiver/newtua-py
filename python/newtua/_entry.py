"""One entry inside an archive."""

# `Archive` below is imported for the type checker only, so the annotation
# naming it must not be evaluated at import time. Python 3.14 defers
# annotations by itself; on 3.11–3.13 — and 3.11 is the floor this package
# declares — this line is what defers them.
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from newtua._stream import EntryStream

if TYPE_CHECKING:
    from newtua._archive import Archive

__all__ = ["Entry", "EntryKind"]


class EntryKind(StrEnum):
    """What an entry is."""

    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"


@dataclass(frozen=True)
class Entry:
    """
    Metadata for one entry, plus shortcuts to its contents.

    `path` is a normalised, decoded name for everyday use. `raw_name` is the
    archive's original bytes — not necessarily valid UTF-8 — and is what
    path-safety checks must inspect; decode via `Archive.detected_encoding`
    if needed.

    `index` uniquely identifies the entry: names may repeat.

    ### Properties:
    - `path`: the UTF-8 name
    - `raw_name`: name as recorded in the archive
    - `index`: uniquely identifies the entry: names may repeat
    - `kind`: the entry's type (`EntryKind` - `FILE`, `DIR`, `SYMLINK`)
    - `size`: the entry's size (in bytes)
    - `is_encrypted`: whether the entry is encrypted
    - `mode`: Unix permission bits or `None` if unavailable.
    - `mtime`: the entry's modification time

    ### Methods:
    - `is_file()`: whether this entry is a regular file
    - `is_dir()`: whether this entry is a directory
    - `is_symlink()`: whether this entry is a symbolic link
    - `read()`: read the whole entry into memory
    - `open()`: open the entry as a file-like object
    - `extract()`: extract the entry into a destination directory
    """

    index: int
    path: PurePosixPath
    raw_name: bytes
    kind: EntryKind
    size: int
    is_encrypted: bool
    mode: int | None
    mtime: datetime | None
    # Back-reference to the owning archive. Excluded from comparison and repr:
    # two entries with the same metadata are the same entry.
    _archive: Archive | None = field(
        default=None, compare=False, repr=False, kw_only=True
    )

    def is_file(self) -> bool:
        """Whether this entry is a regular file."""
        return self.kind is EntryKind.FILE

    def is_dir(self) -> bool:
        """Whether this entry is a directory."""
        return self.kind is EntryKind.DIR

    def is_symlink(self) -> bool:
        """Whether this entry is a symbolic link."""
        return self.kind is EntryKind.SYMLINK

    def _owner(self) -> Archive:
        """The archive that owns this entry."""
        if self._archive is None:
            raise ValueError("this entry is not attached to an archive")
        return self._archive

    def read(self) -> bytes:
        """Read the whole entry into memory."""
        return self._owner().read(self)

    def open(self, *, stream: bool = False) -> EntryStream:
        """Open the entry as a file-like object."""
        return self._owner().open(self, stream=stream)

    def extract(self, dest: str | os.PathLike[str]) -> None:
        """Extract just this entry into `dest`, with no wrapper folder.

        The wrapper exists to keep a whole archive's contents from scattering
        across `dest`. One named entry cannot scatter, so wrapping it in a
        folder named after the archive would only bury it one level deeper
        than asked for.
        """
        self._owner().extract(dest, selection=[self], wrapper=False)
