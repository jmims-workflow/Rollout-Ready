"""Every demo comparison, run through the real checks, scoring and verdict code."""

import pytest

from rollout_ready.compare import build_comparison, example_mix, needs_harder_examples
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

PARTNER = {
    "cobalt-reseller": ("3.5", "4.2", "better"),
    "tidewater-two-contacts": ("3.0", "4.0", "better"),
    "summit-said-no": ("3.5", "3.8", "better"),
    "pinecrest-booth-notes": ("3.2", "3.5", "same"),
    "harlow-cold": ("3.5", "3.8", "same"),
    "brightline-competitor": ("3.8", "3.8", "same"),
}

WEBINAR_V1_V2 = {
    "insight-ops-standard": ("4.0", "4.0", "same"),
    "two-sessions-two-zones": ("3.8", "3.8", "same"),
    "retail-speaker-change": ("3.8", "3.8", "same"),
    "logistics-bare-brief": ("3.8", "4.0", "same"),
    "partner-cohost": ("4.0", "4.0", "same"),
    "healthcare-date-moved": ("3.8", "3.8", "same"),
}

WEBINAR_V2_V3 = {
    "two-sessions-two-zones": ("3.8", "3.8", "worse"),
    "logistics-bare-brief": ("4.0", "3.5", "worse"),
    "healthcare-date-moved": ("3.8", "4.5", "better"),
    "insight-ops-standard": ("4.0", "4.2", "same"),
    "retail-speaker-change": ("3.8", "3.8", "same"),
    "partner-cohost": ("4.0", "3.8", "same"),
}


def rows(comp):
    return {r.case.id: (fmt(r.current_score), fmt(r.new_score), r.result) for r in comp.rows}


def misses(comp):
    return {r.case.id: [c.name for c in r.new_missed] for r in comp.rows if r.new_missed}


# follow-up email (Sales) ---------------------------------------------------------
def test_featured_comparison_v2_to_v3(demo_store):
    comp = build_comparison(demo_store, "followup-email", "v2", "v3", live=False)
    assert rows(comp) == FEATURED
    assert (comp.worse, comp.better, comp.same) == (2, 4, 2)
    assert (fmt(comp.avg_current), fmt(comp.avg_new)) == ("3.6", "3.9")
    assert comp.verdict == "Not ready to roll out"
    assert comp.explanation == ("Version 3 made 4 examples better and 2 examples worse. The average score went up "
                                "from 3.6 to 3.9, which would have hidden both problems.")
    assert [r.case.id for r in comp.rows[:2]] == ["ridgeway-pricing", "calloway-two-asks"]


def test_featured_deal_breaker_callout_and_evidence_line(demo_store):
    comp = build_comparison(demo_store, "followup-email", "v2", "v3", live=False)
    assert misses(comp) == {"ridgeway-pricing": ["Answers every question asked"],
                            "calloway-two-asks": ["Answers every question asked"]}
    assert comp.deal_breaker_callout == (
        'Missed a deal-breaker: "Answers every question asked" on 2 examples. '
        "A missed deal-breaker blocks a rollout, whatever the average says.")
    assert comp.evidence_line == "Based on 8 examples. Even when a version is ready, spot-check real outputs in its first week."
    assert comp.mix_line == "Example mix: 3 everyday, 2 messy, 2 complicated, 1 that broke before."


def test_followup_deal_breakers_and_team(demo_store):
    suite = demo_store.load_suite("followup-email")
    assert suite.team == "Sales"
    assert [c.name for c in suite.rubric if c.deal_breaker] == ["Accurate to the notes", "Answers every question asked"]


def test_followup_example_kinds(demo_store):
    kinds = {c.id: c.kind for c in demo_store.load_cases("followup-email")}
    assert kinds == {
        "ridgeway-pricing": "complicated", "calloway-two-asks": "complicated",
        "brightwater-short-call": "everyday", "kestrel-trial-extension": "everyday", "meridian-insight-demo": "everyday",
        "harbor-point-rambling": "messy", "alder-finch-pause": "messy", "lakeshore-no-show": "broke_before",
    }


@pytest.mark.parametrize("version", ["v2", "v4"])
def test_followup_v2_and_v4_miss_no_deal_breakers(demo_store, version):
    comp = build_comparison(demo_store, "followup-email", "v1", version, live=False)
    assert misses(comp) == {}


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
    assert comp.deal_breaker_callout == ""
    assert comp.row("harbor-point-rambling").checks[0].new.passed
    assert comp.row("ridgeway-pricing").new_words < 120


# partner outreach (Partnerships) ------------------------------------------------
def test_partner_outreach_v1_to_v2_is_ready(demo_store):
    suite = demo_store.load_suite("partner-outreach")
    assert suite.team == "Partnerships"
    assert [c.name for c in suite.rubric if c.deal_breaker] == ["Accurate to the notes"]
    comp = build_comparison(demo_store, "partner-outreach", "v1", "v2", live=False)
    assert rows(comp) == PARTNER
    assert (comp.worse, comp.better, comp.same) == (0, 3, 3)
    assert (fmt(comp.avg_current), fmt(comp.avg_new)) == ("3.4", "3.8")
    assert comp.verdict == "Ready to roll out"
    assert misses(comp) == {}
    assert comp.mix_line == "Example mix: 2 everyday, 1 messy, 2 complicated, 1 that broke before."


def test_partner_outreach_checks_really_flip(demo_store):
    comp = build_comparison(demo_store, "partner-outreach", "v1", "v2", live=False)
    tidewater = comp.row("tidewater-two-contacts")
    assert not tidewater.checks[0].current.passed and tidewater.checks[0].new.passed
    assert tidewater.current_words > 140 >= tidewater.new_words
    summit = comp.row("summit-said-no")
    assert not summit.checks[1].current.passed and summit.checks[1].new.passed
    assert "synergy" in summit.current_output and "synergy" not in summit.new_output


# webinar invite (Marketing) -------------------------------------------------------
def test_webinar_v1_to_v2_no_clear_difference(demo_store):
    suite = demo_store.load_suite("webinar-invite")
    assert suite.team == "Marketing"
    assert [c.name for c in suite.rubric if c.deal_breaker] == ["Event details are right"]
    comp = build_comparison(demo_store, "webinar-invite", "v1", "v2", live=False)
    assert rows(comp) == WEBINAR_V1_V2
    assert (comp.worse, comp.better, comp.same) == (0, 0, 6)
    assert comp.verdict == "No clear difference"
    assert misses(comp) == {}


def test_webinar_v2_to_v3_keep_current_with_missed_deal_breaker(demo_store):
    comp = build_comparison(demo_store, "webinar-invite", "v2", "v3", live=False)
    assert rows(comp) == WEBINAR_V2_V3
    assert (comp.worse, comp.better, comp.same) == (2, 1, 3)
    assert (fmt(comp.avg_current), fmt(comp.avg_new)) == ("3.9", "3.9")
    assert comp.verdict == "Keep the current version"
    assert misses(comp) == {"two-sessions-two-zones": ["Event details are right"]}
    assert comp.deal_breaker_callout.startswith('Missed a deal-breaker: "Event details are right" on 1 example.')


def test_webinar_deal_breaker_makes_an_unchanged_score_worse(demo_store):
    comp = build_comparison(demo_store, "webinar-invite", "v2", "v3", live=False)
    row = comp.row("two-sessions-two-zones")
    assert row.change == 0 and row.result == "worse"
    assert row.headline.startswith("The version you're trying misses a deal-breaker: Event details are right, scored 2")


def test_webinar_edge_case_exactly_at_half_a_point(demo_store):
    comp = build_comparison(demo_store, "webinar-invite", "v2", "v3", live=False)
    row = comp.row("logistics-bare-brief")
    assert row.change == -0.5 and row.result == "worse" and not row.new_missed


def test_webinar_outputs_really_pass_every_check(demo_store):
    for current, new in (("v1", "v2"), ("v2", "v3")):
        comp = build_comparison(demo_store, "webinar-invite", current, new, live=False)
        for r in comp.rows:
            assert all(c.current.passed and c.new.passed for c in r.checks), (current, new, r.case.id)


# every prompt ----------------------------------------------------------------------
@pytest.mark.parametrize("suite,current,new,verdict,counts", [
    ("followup-email", "v1", "v2", "Ready to roll out", None),
    ("followup-email", "v2", "v3", "Not ready to roll out", (2, 4, 2)),
    ("followup-email", "v2", "v4", "Ready to roll out", (0, 4, 4)),
    ("partner-outreach", "v1", "v2", "Ready to roll out", (0, 3, 3)),
    ("webinar-invite", "v1", "v2", "No clear difference", (0, 0, 6)),
    ("webinar-invite", "v2", "v3", "Keep the current version", (2, 1, 3)),
])
def test_every_demo_comparison(demo_store, suite, current, new, verdict, counts):
    comp = build_comparison(demo_store, suite, current, new, live=False)
    assert comp.complete
    assert comp.verdict == verdict
    if counts:
        assert (comp.worse, comp.better, comp.same) == counts


def test_all_four_verdicts_reachable_in_demo(demo_store):
    pairs = [("followup-email", "v2", "v4"), ("followup-email", "v2", "v3"),
             ("webinar-invite", "v1", "v2"), ("webinar-invite", "v2", "v3")]
    verdicts = {build_comparison(demo_store, *p, live=False).verdict for p in pairs}
    assert verdicts == {"Ready to roll out", "Not ready to roll out", "No clear difference", "Keep the current version"}


def test_demo_prompts_have_teams_and_a_mix(demo_store):
    assert {s.id: s.team for s in demo_store.suites()} == {
        "followup-email": "Sales", "partner-outreach": "Partnerships", "webinar-invite": "Marketing"}
    for sid in demo_store.suite_ids():
        cases = demo_store.load_cases(sid)
        assert not needs_harder_examples(cases), sid
        assert "broke before" in example_mix(cases), sid


def test_every_recorded_version_covers_every_case(demo_store):
    for sid in demo_store.suite_ids():
        cases = {c.id for c in demo_store.load_cases(sid)}
        for v in demo_store.versions(sid):
            rec = demo_store.load_recorded(sid, v.id)
            assert rec is not None, (sid, v.id)
            assert set(rec.cases) == cases, (sid, v.id)
