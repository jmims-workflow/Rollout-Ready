"""Every demo comparison, run through the real checks, scoring and verdict code."""

import pytest

from rollout_ready.compare import build_comparison
from rollout_ready.scoring import fmt

FEATURED = {
    "ridgeway-pricing": ("4.0", "2.8", "worse"),
    "calloway-two-asks": ("3.8", "3.0", "worse"),
    "brightwater-short-call": ("3.4", "4.6", "better"),
    "harbor-point-rambling": ("3.0", "4.4", "better"),
    "alder-finch-pause": ("3.4", "4.0", "better"),
    "kestrel-trial-extension": ("3.6", "4.2", "better"),
    "meridian-insight-demo": ("4.0", "4.0", "same"),
    "lakeshore-no-show": ("3.6", "4.0", "same"),
}

SAVED_EXAMPLE = {
    "ridgeway-pricing": ("4.0", "4.2", "same"),
    "calloway-two-asks": ("3.8", "4.0", "same"),
    "brightwater-short-call": ("3.4", "4.4", "better"),
    "harbor-point-rambling": ("3.0", "4.2", "better"),
    "alder-finch-pause": ("3.4", "4.0", "better"),
    "kestrel-trial-extension": ("3.6", "4.2", "better"),
    "meridian-insight-demo": ("4.0", "4.0", "same"),
    "lakeshore-no-show": ("3.6", "3.8", "same"),
}


def rows(comp):
    return {r.case.id: (fmt(r.current_score), fmt(r.new_score), r.result) for r in comp.rows}


def test_featured_comparison_v2_to_v3(demo_store):
    comp = build_comparison(demo_store, "followup-email", "v2", "v3", live=False)
    assert rows(comp) == FEATURED
    assert (comp.worse, comp.better, comp.same) == (2, 4, 2)
    assert (fmt(comp.avg_current), fmt(comp.avg_new)) == ("3.6", "3.9")
    assert comp.verdict == "Not ready to roll out"
    assert comp.explanation == ("Version 3 made 4 cases better and 2 cases worse. The average score went up "
                                "from 3.6 to 3.9, which would have hidden both problems.")
    assert [r.case.id for r in comp.rows[:2]] == ["ridgeway-pricing", "calloway-two-asks"]


def test_featured_harbor_point_word_limit_flip(demo_store):
    comp = build_comparison(demo_store, "followup-email", "v2", "v3", live=False)
    row = comp.row("harbor-point-rambling")
    word_check = row.checks[0]
    assert not word_check.current.passed and word_check.new.passed


def test_featured_ridgeway_scores(demo_store):
    comp = build_comparison(demo_store, "followup-email", "v2", "v3", live=False)
    row = comp.row("ridgeway-pricing")
    assert [c.current for c in row.criteria] == [4, 4, 4, 3, 5]
    assert [c.new for c in row.criteria] == [4, 1, 3, 4, 2]
    assert row.new_words <= 45
    assert row.biggest_drop.criterion.id == "answers"
    assert all(c.new_reason for c in row.criteria)


def test_saved_example_v2_to_v4(demo_store):
    comp = build_comparison(demo_store, "followup-email", "v2", "v4", live=False)
    assert rows(comp) == SAVED_EXAMPLE
    assert (comp.worse, comp.better, comp.same) == (0, 4, 4)
    assert (fmt(comp.avg_current), fmt(comp.avg_new)) == ("3.6", "4.1")
    assert comp.verdict == "Ready to roll out"
    assert comp.row("harbor-point-rambling").checks[0].new.passed
    assert comp.row("ridgeway-pricing").new_words < 120


@pytest.mark.parametrize("suite,current,new,verdict,counts", [
    ("followup-email", "v1", "v2", "Ready to roll out", None),
    ("followup-email", "v2", "v3", "Not ready to roll out", (2, 4, 2)),
    ("followup-email", "v2", "v4", "Ready to roll out", (0, 4, 4)),
    ("ticket-summary", "v1", "v2", "Ready to roll out", (0, 3, 3)),
    ("release-notes", "v1", "v2", "No clear difference", (0, 0, 6)),
    ("release-notes", "v2", "v3", "Keep the current version", (2, 1, 3)),
])
def test_every_demo_comparison(demo_store, suite, current, new, verdict, counts):
    comp = build_comparison(demo_store, suite, current, new, live=False)
    assert comp.complete
    assert comp.verdict == verdict
    if counts:
        assert (comp.worse, comp.better, comp.same) == counts


def test_all_four_verdicts_reachable_in_demo(demo_store):
    pairs = [("followup-email", "v2", "v4"), ("followup-email", "v2", "v3"),
             ("release-notes", "v1", "v2"), ("release-notes", "v2", "v3")]
    verdicts = {build_comparison(demo_store, *p, live=False).verdict for p in pairs}
    assert verdicts == {"Ready to roll out", "Not ready to roll out", "No clear difference", "Keep the current version"}


def test_release_notes_edge_case_exactly_at_threshold(demo_store):
    comp = build_comparison(demo_store, "release-notes", "v2", "v3", live=False)
    row = comp.row("shift-swap-approvals")
    assert row.change == -0.5 and row.result == "worse"


def test_every_recorded_version_covers_every_case(demo_store):
    for sid in demo_store.suite_ids():
        cases = {c.id for c in demo_store.load_cases(sid)}
        for v in demo_store.versions(sid):
            rec = demo_store.load_recorded(sid, v.id)
            assert rec is not None, (sid, v.id)
            assert set(rec.cases) == cases, (sid, v.id)
