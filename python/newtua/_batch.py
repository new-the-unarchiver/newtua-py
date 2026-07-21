"""Batch tools: extract or list many archives in parallel.

The thread backend drives `extract_path`/`list_path`, which release the GIL, so
threads run on real cores. Batch calls never raise on the first failure — each
job's outcome (a Report or the caught exception) is collected.
"""

import concurrent.futures as cf
import functools
import os
import threading
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterable, Sequence, TypeVar

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
    try:
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
    except Exception as exc:
        raise _as_typed(exc)
    return Report(extracted=r.extracted, failed=r.failed, aborted=r.aborted)


_T = TypeVar("_T")


def _make_executor(backend: str, max_workers: int) -> cf.Executor:
    if backend == "thread":
        return cf.ThreadPoolExecutor(max_workers=max_workers)
    if backend == "process":
        return cf.ProcessPoolExecutor(max_workers=max_workers)
    raise ValueError(f"unknown backend {backend!r}; use 'thread' or 'process'")


def _run_backend(
    backend: str,
    max_workers: int | None,
    tasks: Sequence[tuple[int, Callable[[], _T]]],
    cancel: "threading.Event | None" = None,
) -> list[tuple[int, _T | None, BaseException | None]]:
    """Run `tasks` (index, thunk) on the chosen backend; collect (index, ok, err).
    Never raises on a single task's failure.

    `cancel`: cooperative "stop submitting new tasks" — checked before each
    submit. Already-submitted tasks always run to completion; tasks not yet
    submitted when `cancel` is set are simply absent from the result."""
    workers = max_workers or os.cpu_count() or 1
    out: list[tuple[int, _T | None, BaseException | None]] = []
    with _make_executor(backend, workers) as ex:
        futures = {}
        for i, thunk in tasks:
            if cancel is not None and cancel.is_set():
                break
            futures[ex.submit(thunk)] = i
        for fut in cf.as_completed(futures):
            i = futures[fut]
            try:
                out.append((i, fut.result(), None))
            except Exception as exc:
                out.append((i, None, _as_typed(exc)))
    return out


def extract_many(
    jobs: Iterable["ExtractJob | tuple[_ArchiveRef, _ArchiveRef]"],
    *,
    backend: str = "thread",
    max_workers: int | None = None,
    on_result: Callable[[ExtractJob, Report], None] | None = None,
    on_error: Callable[[ExtractJob, BaseException], None] | None = None,
    cancel: "threading.Event | None" = None,
) -> list[BatchResult]:
    """Extract many archives in parallel; never raises on a single failure.

    `backend`: "thread" (default, GIL released → real cores) or "process".
    `backend="process"` rejects any job with a per-job `progress` callback
    up front, with a `ValueError`, before submitting anything.

    `on_result`/`on_error` are called from the calling thread, once all
    results have been collected (order follows completion order via
    `as_completed`). Only per-job `progress` runs on a worker thread.

    `cancel`: an optional `threading.Event`. If set, stops submitting new
    jobs (already-submitted jobs finish normally); cancelled jobs are simply
    absent from the returned list.
    """
    normalised = [_normalise_job(j) for j in jobs]
    if backend == "process":
        for job in normalised:
            if job.progress is not None:
                raise ValueError(
                    "progress callbacks are not supported with backend='process'; "
                    "use backend='thread'"
                )
    tasks = [(i, functools.partial(_extract_one, job)) for i, job in enumerate(normalised)]
    raw = _run_backend(backend, max_workers, tasks, cancel)
    results: list[BatchResult | None] = [None] * len(normalised)
    for i, report, error in raw:
        job = normalised[i]
        results[i] = BatchResult(job, report, error)
        if error is None and on_result is not None:
            on_result(job, report)  # type: ignore[arg-type]
        elif error is not None and on_error is not None:
            on_error(job, error)
    return [r for r in results if r is not None]


@dataclass(frozen=True)
class ListingResult:
    """Outcome of one listing: exactly one of `entries`/`error` is set."""

    archive: _ArchiveRef
    entries: tuple[Entry, ...] | None
    error: BaseException | None


def _list_one(archive: _ArchiveRef, password: str | None, encoding: str | None) -> tuple[Entry, ...]:
    """List one archive's entries (metadata only). Module-level for pickling."""
    try:
        raw, _, _ = _newtua.list_path(str(archive), password, encoding)
    except Exception as exc:
        raise _as_typed(exc)
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
    cancel: "threading.Event | None" = None,
) -> list[ListingResult]:
    """List many archives in parallel; never raises on a single failure.

    `backend`: "thread" (default) or "process".

    `on_result`/`on_error` are called from the calling thread, once all
    results have been collected (order follows completion order via
    `as_completed`).

    `cancel`: an optional `threading.Event`. If set, stops submitting new
    listings (already-submitted ones finish normally); cancelled listings
    are simply absent from the returned list.
    """
    items = list(archives)
    tasks = [
        (i, functools.partial(_list_one, arc, password, encoding))
        for i, arc in enumerate(items)
    ]
    raw = _run_backend(backend, max_workers, tasks, cancel)
    results: list[ListingResult | None] = [None] * len(items)
    for i, entries, error in raw:
        arc = items[i]
        results[i] = ListingResult(arc, entries, error)
        if error is None and on_result is not None:
            on_result(arc, entries)  # type: ignore[arg-type]
        elif error is not None and on_error is not None:
            on_error(arc, error)
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
