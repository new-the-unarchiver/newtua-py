import pytest
from dataclasses import fields
from pathlib import PurePosixPath

from newtua._events import (
    BytesWritten,
    EntryFinished,
    EntryStarted,
    EventKind,
    event_from_raw,
)


def test_kind_is_a_class_attribute_not_a_field():
    e = EntryStarted(index=0, path=PurePosixPath("a.txt"), size=8)
    assert [f.name for f in fields(e)] == ["index", "path", "size"]
    assert e.kind is EventKind.STARTED


def test_events_compare_by_value():
    a = EntryStarted(index=0, path=PurePosixPath("a.txt"), size=8)
    b = EntryStarted(index=0, path=PurePosixPath("a.txt"), size=8)
    assert a == b


def test_from_raw_builds_the_right_class():
    started = event_from_raw("start", 0, "a.txt", 0, 8)
    assert started == EntryStarted(index=0, path=PurePosixPath("a.txt"), size=8)
    assert event_from_raw("bytes", 0, None, 5, 0) == BytesWritten(index=0, written=5)
    assert event_from_raw("done", 0, None, 0, 0) == EntryFinished(index=0)


def test_append_works_as_a_bare_callback():
    seen: list = []
    seen.append(event_from_raw("start", 0, "a.txt", 0, 8))
    assert isinstance(seen[0], EntryStarted)


def test_unknown_event_type_raises_value_error():
    with pytest.raises(ValueError, match="Unknown event type: 'unknown'"):
        event_from_raw("unknown", 0, None, 0, 0)
