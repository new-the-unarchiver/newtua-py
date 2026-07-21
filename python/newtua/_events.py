"""
Progress events reported while extracting.

Separate classes rather than one class with optional fields: `match` narrows the
type, so a handler can reach `event.path` without a `None` check.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Callable, ClassVar

__all__ = [
    "BytesWritten",
    "EntryFinished",
    "EntryStarted",
    "EventKind",
    "ProgressEvent",
]


class EventKind(StrEnum):
    """
    Which kind of progress event this is.

    The values are the engine's own tags, not a second vocabulary alongside
    them: `event_from_raw` looks the incoming tag up here, so a tag the enum
    does not list is by definition unknown.
    """

    STARTED = "start"
    BYTES = "bytes"
    FINISHED = "done"


@dataclass(frozen=True)
class ProgressEvent:
    """Base class for progress events. `index` is the entry's position."""

    kind: ClassVar[EventKind]
    index: int


@dataclass(frozen=True)
class EntryStarted(ProgressEvent):
    """Extraction of an entry has begun."""

    kind: ClassVar[EventKind] = EventKind.STARTED
    path: PurePosixPath
    size: int


@dataclass(frozen=True)
class BytesWritten(ProgressEvent):
    """Some bytes of the current entry have been written."""

    kind: ClassVar[EventKind] = EventKind.BYTES
    written: int


@dataclass(frozen=True)
class EntryFinished(ProgressEvent):
    """Extraction of an entry has finished."""

    kind: ClassVar[EventKind] = EventKind.FINISHED


def _build_started(
    index: int, path: str | None, written: int, size: int
) -> EntryStarted:
    """Build an `EntryStarted` event."""
    return EntryStarted(index=index, path=PurePosixPath(path or ""), size=size)


def _build_bytes(
    index: int, path: str | None, written: int, size: int
) -> BytesWritten:
    """Build a `BytesWritten` event."""
    return BytesWritten(index=index, written=written)


def _build_finished(
    index: int, path: str | None, written: int, size: int
) -> EntryFinished:
    """Build an `EntryFinished` event."""
    return EntryFinished(index=index)


#: The one table: each kind, and how to build its event from the five values
#: the compiled module sends. Every class listed here carries the same kind as
#: its key, so the enum and the dispatch can never drift apart.
_BUILDERS: dict[EventKind, Callable[[int, str | None, int, int], ProgressEvent]] = {
    EventKind.STARTED: _build_started,
    EventKind.BYTES: _build_bytes,
    EventKind.FINISHED: _build_finished,
}


def event_from_raw(
    event: str, index: int, path: str | None, written: int, size: int
) -> ProgressEvent:
    """
    Build an event object from the five values the compiled module sends.

    The Rust module and this code always ship together, so version mismatch
    is impossible for users. Only dev can introduce a new event type; we fail
    loudly rather than silently substituting the wrong class.
    """
    try:
        kind = EventKind(event)
    except ValueError:
        raise ValueError(f"Unknown event type: {event!r}") from None
    return _BUILDERS[kind](index, path, written, size)
