"""Batch tools: extract or list many archives in parallel.

The thread backend drives `extract_path`/`list_path`, which release the GIL, so
threads run on real cores. Batch calls never raise on the first failure — each
job's outcome (a Report or the caught exception) is collected.
"""

import concurrent.futures as cf
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterable, Sequence

from newtua import _newtua
from newtua._archive import (
    Report,
    _entries_from_raw,
    _resolve_index,
    _wrap_progress,
)
from newtua._entry import Entry
from newtua._errors import raise_for
from newtua._events import ProgressEvent

__all__ = ["ExtractJob", "BatchResult", "extract_many", "ListingResult", "list_many"]

_ArchiveRef = str | os.PathLike[str]
_Selection = Sequence[int | str | PurePosixPath | Entry]


@dataclass(frozen=True)
class ExtractJob:
    """One archive to extract, with its own options."""

    archive: _ArchiveRef
    dest: _ArchiveRef
    password: str | None = None
    encoding: str | None = None
    selection: _Selection | None = None
    wrapper: bool = True
    strict: bool = False
    preserve: bool = True
    progress: Callable[[ProgressEvent], bool | None] | None = None


@dataclass(frozen=True)
class BatchResult:
    """Outcome of one job: exactly one of `report`/`error` is set."""

    job: ExtractJob
    report: Report | None
    error: BaseException | None


def _normalise_job(job: "ExtractJob | tuple[_ArchiveRef, _ArchiveRef]") -> ExtractJob:
    if isinstance(job, ExtractJob):
        return job
    archive, dest = job  # tuple shorthand
    return ExtractJob(archive=archive, dest=dest)


def _selection_indices(job: ExtractJob) -> list[int] | None:
    """Resolve a job's selection to indices, listing the archive only if a name
    (not a plain index) is present."""
    if job.selection is None:
        return None
    if all(isinstance(ref, int) for ref in job.selection):
        return [int(ref) for ref in job.selection]  # type: ignore[arg-type]
    raw, _, _ = _newtua.list_path(str(job.archive), job.password, job.encoding)
    entries, _ = _entries_from_raw(raw, None)
    return [_resolve_index(entries, ref) for ref in job.selection]


def _extract_one(job: ExtractJob) -> Report:
    """Run one extraction via the GIL-releasing primitive. Module-level so the
    process backend (spawn) can pickle it."""
    raw_progress = _wrap_progress(job.progress) if job.progress is not None else None
    r = _newtua.extract_path(
        str(job.archive),
        str(job.dest),
        _selection_indices(job),
        job.wrapper,
        job.strict,
        job.preserve,
        raw_progress,
        os.fspath(job.archive),
        job.password,
        job.encoding,
    )
    return Report(extracted=r.extracted, failed=r.failed, aborted=r.aborted)


def extract_many(
    jobs: Iterable["ExtractJob | tuple[_ArchiveRef, _ArchiveRef]"],
    *,
    backend: str = "thread",
    max_workers: int | None = None,
    on_result: Callable[[ExtractJob, Report], None] | None = None,
    on_error: Callable[[ExtractJob, BaseException], None] | None = None,
) -> list[BatchResult]:
    """Extract many archives in parallel; never raises on a single failure.

    `backend`: "thread" (default, GIL released → real cores) or "process".
    Callbacks fire as each job finishes; under the thread backend they may run
    on a worker thread — keep them thread-safe.
    """
    normalised = [_normalise_job(j) for j in jobs]
    workers = max_workers or os.cpu_count() or 1
    results: list[BatchResult | None] = [None] * len(normalised)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_extract_one, job): (i, job) for i, job in enumerate(normalised)}
        for fut in cf.as_completed(futures):
            i, job = futures[fut]
            try:
                report = fut.result()
                results[i] = BatchResult(job, report, None)
                if on_result is not None:
                    on_result(job, report)
            except Exception as exc:
                mapped = _as_typed(exc)
                results[i] = BatchResult(job, None, mapped)
                if on_error is not None:
                    on_error(job, mapped)
    return [r for r in results if r is not None]


@dataclass(frozen=True)
class ListingResult:
    """Outcome of one listing: exactly one of `entries`/`error` is set."""

    archive: _ArchiveRef
    entries: tuple[Entry, ...] | None
    error: BaseException | None


def _list_one(archive: _ArchiveRef, password: str | None, encoding: str | None) -> tuple[Entry, ...]:
    """List one archive's entries (metadata only). Module-level for pickling."""
    raw, _, _ = _newtua.list_path(str(archive), password, encoding)
    entries, _ = _entries_from_raw(raw, None)
    return entries


def list_many(
    archives: Iterable[_ArchiveRef],
    *,
    password: str | None = None,
    encoding: str | None = None,
    backend: str = "thread",
    max_workers: int | None = None,
    on_result: Callable[[_ArchiveRef, tuple[Entry, ...]], None] | None = None,
    on_error: Callable[[_ArchiveRef, BaseException], None] | None = None,
) -> list[ListingResult]:
    """List many archives in parallel; never raises on a single failure."""
    items = list(archives)
    workers = max_workers or os.cpu_count() or 1
    results: list[ListingResult | None] = [None] * len(items)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_list_one, arc, password, encoding): (i, arc)
            for i, arc in enumerate(items)
        }
        for fut in cf.as_completed(futures):
            i, arc = futures[fut]
            try:
                entries = fut.result()
                results[i] = ListingResult(arc, entries, None)
                if on_result is not None:
                    on_result(arc, entries)
            except Exception as exc:
                mapped = _as_typed(exc)
                results[i] = ListingResult(arc, None, mapped)
                if on_error is not None:
                    on_error(arc, mapped)
    return [r for r in results if r is not None]


def _as_typed(exc: BaseException) -> BaseException:
    """Map a compiled-module exception to its typed counterpart; pass anything
    else through unchanged.

    Only exceptions carrying a `kind` come from the engine — map those. A plain
    Python exception (a genuine bug) is returned as-is, never masked as a
    `NewtuaError`."""
    if getattr(exc, "kind", None) is None:
        return exc
    try:
        raise_for(exc)
    except BaseException as typed:  # raise_for always raises
        return typed
