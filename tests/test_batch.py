import pathlib
import pickle
import threading

import pytest

import newtua
from newtua import ExtractJob, extract_many, list_many
from tests.conftest import make_two_entry_zip

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_extract_many_unpacks_all(tmp_path):
    zips = []
    for i in range(4):
        z = make_two_entry_zip(tmp_path / f"in{i}.zip")
        zips.append(ExtractJob(archive=z, dest=tmp_path / f"out{i}"))
    results = extract_many(zips)
    assert len(results) == 4
    assert all(r.error is None and r.report.extracted == 2 for r in results)
    for i in range(4):
        assert (tmp_path / f"out{i}" / "in{}".format(i) / "a.txt").read_bytes() == b"a"


def test_extract_many_tuple_shorthand(tmp_path):
    z = make_two_entry_zip(tmp_path / "in.zip")
    (result,) = extract_many([(z, tmp_path / "out")])
    assert result.error is None and result.report.extracted == 2


def test_extract_many_isolates_errors(tmp_path):
    good = make_two_entry_zip(tmp_path / "good.zip")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not an archive at all")
    results = extract_many(
        [ExtractJob(archive=good, dest=tmp_path / "g"),
         ExtractJob(archive=bad, dest=tmp_path / "b")]
    )
    by_name = {pathlib.Path(r.job.archive).name: r for r in results}
    assert by_name["good.zip"].report.extracted == 2
    assert by_name["good.zip"].error is None
    assert by_name["bad.zip"].report is None
    assert isinstance(by_name["bad.zip"].error, newtua.NewtuaError)


def test_extract_many_callbacks_fire(tmp_path):
    z = make_two_entry_zip(tmp_path / "in.zip")
    seen = []
    extract_many(
        [ExtractJob(archive=z, dest=tmp_path / "out")],
        on_result=lambda job, report: seen.append(("ok", report.extracted)),
        on_error=lambda job, exc: seen.append(("err", exc)),
    )
    assert seen == [("ok", 2)]


def test_list_many_matches_sync(tmp_path):
    archives = [make_two_entry_zip(tmp_path / f"in{i}.zip") for i in range(3)]
    results = list_many(archives)
    assert len(results) == 3
    for r in results:
        assert r.error is None
        with newtua.Archive(str(r.archive)) as sync:
            assert [str(e.path) for e in r.entries] == [str(e.path) for e in sync]


def test_extract_many_process_backend(tmp_path):
    zips = [ExtractJob(archive=make_two_entry_zip(tmp_path / f"in{i}.zip"),
                       dest=tmp_path / f"out{i}") for i in range(3)]
    results = extract_many(zips, backend="process")
    assert all(r.error is None and r.report.extracted == 2 for r in results)


def test_process_backend_rejects_progress(tmp_path):
    z = make_two_entry_zip(tmp_path / "in.zip")
    job = ExtractJob(archive=z, dest=tmp_path / "out", progress=lambda ev: None)
    with pytest.raises(ValueError, match="progress"):
        extract_many([job], backend="process")


def test_unknown_backend_is_rejected(tmp_path):
    z = make_two_entry_zip(tmp_path / "in.zip")
    with pytest.raises(ValueError, match="backend"):
        extract_many([(z, tmp_path / "out")], backend="nonsense")


def test_extract_many_process_backend_maps_errors(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not an archive")
    (result,) = extract_many(
        [ExtractJob(archive=bad, dest=tmp_path / "out")], backend="process"
    )
    assert result.report is None
    assert isinstance(result.error, newtua.UnknownFormatError)
    assert isinstance(result.error, newtua.NewtuaError)


def test_list_many_process_backend_maps_errors(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not an archive")
    (result,) = list_many([bad], backend="process")
    assert result.entries is None
    assert isinstance(result.error, newtua.UnknownFormatError)


def test_archive_cannot_be_pickled(tmp_path):
    z = make_two_entry_zip(tmp_path / "in.zip")
    with pytest.raises(TypeError, match="cannot be sent to another process"):
        pickle.dumps(newtua.Archive(str(z)))


def test_async_archive_cannot_be_pickled(tmp_path):
    z = make_two_entry_zip(tmp_path / "in.zip")
    with pytest.raises(TypeError, match="cannot be sent to another process"):
        pickle.dumps(newtua.AsyncArchive(str(z)))


def test_extract_many_cancel_stops_submitting(tmp_path):
    ev = threading.Event()
    ev.set()
    zips = [ExtractJob(archive=make_two_entry_zip(tmp_path / f"in{i}.zip"),
                       dest=tmp_path / f"out{i}") for i in range(3)]
    results = extract_many(zips, cancel=ev)
    assert len(results) < len(zips)


try:
    import gevent  # noqa: F401

    HAS_GEVENT = True
except ImportError:
    HAS_GEVENT = False

needs_gevent = pytest.mark.skipif(not HAS_GEVENT, reason="gevent not installed")


@needs_gevent
def test_extract_many_under_gevent(tmp_path):
    import gevent.monkey
    if not gevent.monkey.is_module_patched("threading"):
        pytest.skip("run this module under `python -m gevent.monkey ...` for full coverage")
    from newtua._batch import _GeventExecutor, _make_executor
    assert isinstance(_make_executor("thread", 4), _GeventExecutor)
    zips = [ExtractJob(archive=make_two_entry_zip(tmp_path / f"in{i}.zip"),
                       dest=tmp_path / f"out{i}") for i in range(2)]
    results = extract_many(zips, backend="thread")
    assert all(r.error is None for r in results)
