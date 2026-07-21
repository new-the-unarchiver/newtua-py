import pathlib

import newtua
from newtua._batch import ExtractJob, extract_many
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
