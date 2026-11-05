"""The storage interface: write-once records, compare-and-swap updates, listing, and no lost updates."""

import threading

import pytest

from rollout_ready.records import LocalRecordStore, RecordError, set_lock
from rollout_ready.storage import Store, SuiteError


@pytest.fixture
def rs(tmp_path):
    return LocalRecordStore(tmp_path / "records")


def test_read_missing_is_none(rs):
    assert rs.read("evaluations/nothing") is None


def test_write_once_never_replaces(rs):
    assert rs.write_once("answers/abc", {"text": "first"}) is True
    assert rs.write_once("answers/abc", {"text": "second"}) is False
    assert rs.read("answers/abc") == {"text": "first"}


def test_compare_and_swap_refuses_a_stale_version(rs):
    rs.update("releases", lambda cur: [*(cur or []), "a"])
    seen = rs.version("releases")
    rs.update("releases", lambda cur: [*cur, "b"])  # someone else writes first
    assert rs.compare_and_swap("releases", seen, ["a", "stale"]) is False
    assert rs.read("releases") == ["a", "b"]
    assert rs.compare_and_swap("releases", rs.version("releases"), ["a", "b", "c"]) is True
    assert rs.read("releases") == ["a", "b", "c"]


def test_compare_and_swap_on_a_missing_record(rs):
    assert rs.compare_and_swap("tokens", None, {"t1": "used"}) is True
    assert rs.compare_and_swap("tokens", None, {"t2": "used"}) is False


def test_list_by_prefix(rs):
    rs.write_once("evaluations/b", {})
    rs.write_once("evaluations/a", {})
    rs.write_once("answers/x", {})
    assert rs.list("evaluations") == ["evaluations/a", "evaluations/b"]
    assert rs.list("reviews") == []
    assert sorted(rs.list()) == ["answers/x", "evaluations/a", "evaluations/b"]


@pytest.mark.parametrize("key", ["", "../escape", "a/../b", "/abs", "has space", "evaluations/"])
def test_bad_keys_are_refused(rs, key):
    with pytest.raises(RecordError):
        rs.read(key)


def test_no_temporary_files_left_behind(rs):
    rs.write_once("answers/abc", {"x": 1})
    rs.write_once("answers/abc", {"x": 2})
    rs.update("releases", lambda cur: [1])
    leftovers = [p for p in rs.root.rglob("*") if p.name.startswith(".tmp-")]
    assert leftovers == []


# test 15: concurrent updates do not lose changes ------------------------------------
def test_concurrent_updates_lose_nothing(rs):
    threads, per_thread = 8, 25

    def work(n):
        for i in range(per_thread):
            rs.update("releases", lambda cur, n=n, i=i: [*(cur or []), f"{n}-{i}"])

    pool = [threading.Thread(target=work, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    events = rs.read("releases")
    assert len(events) == threads * per_thread
    assert len(set(events)) == threads * per_thread


def test_concurrent_write_once_has_one_winner(rs):
    wins = []

    def work(n):
        if rs.write_once("evaluations/same", {"by": n}):
            wins.append(n)

    pool = [threading.Thread(target=work, args=(n,)) for n in range(10)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert len(wins) == 1
    assert rs.read("evaluations/same") == {"by": wins[0]}


def test_set_lock_is_one_lock_per_set():
    assert set_lock("followup-email") is set_lock("followup-email")
    assert set_lock("followup-email") is not set_lock("webinar-invite")


# the Store accessor ----------------------------------------------------------------
def test_store_records_live_under_runs(suites_copy):
    store = Store(suites_copy)
    rs = store.records("followup-email")
    rs.write_once("evaluations/e1", {"ok": True})
    assert (suites_copy / "followup-email" / "runs" / "evaluations" / "e1.json").exists()
    with pytest.raises(SuiteError):
        store.records("../etc")
