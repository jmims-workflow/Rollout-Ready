"""Overrides on evidence (spec tests 6 and 7): they change an example's result, never a missed deal-breaker,
and never carry over to changed evidence. Removing one appends an event."""

import time

import pytest
from fastapi.testclient import TestClient

from conftest import SUITES, FakeProvider, snapshot
from rollout_ready import evaluate, scoring
from rollout_ready.compare import build_comparison, comparison_for_record
from rollout_ready.storage import Store
from rollout_ready.web.app import create_app

SUITE = "followup-email"
REVIEWER = "claude-sonnet-5-5"


class Waits(list):
    def __call__(self, seconds):
        self.append(seconds)


def overrides_for(events, comp):
    return evaluate.as_overrides(evaluate.active_override_events(events, comp.record.fingerprint))


def call(case_id, comp, result="same", reason="The answer is there, further down.", remove=False):
    return evaluate.override_event(comp.record.fingerprint, comp.record.id, case_id, call="not_worse",
                                   result=result, reason=reason, remove=remove)


# test 6: an override changes the result but never removes a deal-breaker miss -----------
def test_override_changes_the_result_but_the_deal_breaker_miss_stays(demo_store):
    comp = build_comparison(demo_store, SUITE, "v2", "v3", live=False)
    scores_before = [(c.criterion.id, c.current, c.new) for c in comp.row("ridgeway-pricing").criteria]
    events = [call("ridgeway-pricing", comp), call("calloway-two-asks", comp)]
    comp.apply_overrides(overrides_for(events, comp))
    row = comp.row("ridgeway-pricing")
    assert row.ai_result == scoring.WORSE and row.result == scoring.SAME  # the person's call counts
    assert [c.id for c in row.blocking_misses] == ["answers"]  # the miss is still there
    assert [(c.criterion.id, c.current, c.new) for c in row.criteria] == scores_before  # scores never change
    assert comp.worse == 0 and comp.better == 4
    assert comp.deal_breaker_callout.startswith('Missed a deal-breaker: "Answers every question asked" on 2 examples.')
    assert comp.verdict == "Not ready to roll out"  # without the misses this would read Ready to roll out


def test_override_can_turn_ready_into_needs_review(demo_store):
    comp = build_comparison(demo_store, SUITE, "v2", "v4", live=False)
    assert comp.verdict == "Ready to roll out"
    comp.apply_overrides(overrides_for([call("meridian-insight-demo", comp, result="worse")], comp))
    assert (comp.worse, comp.better) == (1, 4) and comp.verdict == "Needs review"


def test_a_new_record_lists_the_overrides_in_force_and_counts_them(suites_copy):
    store = Store(suites_copy)
    v = store.add_version(SUITE, "A new version", "Try it")
    fp = evaluate.current_fingerprint(store, SUITE, "v2", v.id, REVIEWER)
    store.append_override_event(SUITE, evaluate.override_event(fp, "", "ridgeway-pricing", call="other",
                                                               result="worse", reason="Promises a discount."))
    rec = evaluate.run_evaluation(store, SUITE, "v2", v.id, FakeProvider(), REVIEWER, sleep=Waits()).record
    assert [(o.case_id, o.result, o.reason) for o in rec.overrides_applied] == [
        ("ridgeway-pricing", "worse", "Promises a discount.")]
    assert rec.classifications["ridgeway-pricing"] == "same"  # the AI reviewer's call is kept as it was
    assert rec.counts["worse"] == 1 and rec.verdict == "Not ready to roll out"


# test 7: old overrides do not apply to changed evidence -----------------------------------
def test_overrides_do_not_carry_over_to_changed_evidence(suites_copy):
    store = Store(suites_copy)
    v = store.add_version(SUITE, "A new version", "Try it")
    first = evaluate.run_evaluation(store, SUITE, "v2", v.id, FakeProvider(), REVIEWER, sleep=Waits()).record
    store.append_override_event(SUITE, evaluate.override_event(first.fingerprint, first.id, "ridgeway-pricing",
                                                               call="other", result="worse", reason="Wrong tone."))
    events = store.override_events(SUITE)
    same = build_comparison(store, SUITE, "v2", v.id, live=True)
    assert same.record.fingerprint == first.fingerprint
    assert same.apply_overrides(overrides_for(events, same)).row("ridgeway-pricing").result == "worse"

    path = suites_copy / SUITE / "suite.yaml"
    path.write_text(path.read_text().replace("name: Sounds like a person", "name: Sounds human"))
    evaluate.run_evaluation(store, SUITE, "v2", v.id, FakeProvider(), REVIEWER, sleep=Waits())
    changed = build_comparison(store, SUITE, "v2", v.id, live=True)
    assert changed.record.fingerprint != first.fingerprint
    changed.apply_overrides(overrides_for(store.override_events(SUITE), changed))
    assert changed.row("ridgeway-pricing").override is None and not changed.overridden_rows

    # The override stays in the history, and the old record still shows it.
    assert [e.reason for e in store.override_events(SUITE)] == ["Wrong tone."]
    old = comparison_for_record(store, SUITE, first.id)
    assert old.apply_overrides(overrides_for(store.override_events(SUITE), old)).row("ridgeway-pricing").result == "worse"


def _wait(client):
    for _ in range(300):
        if all(j.finished for j in client.app.state.jobs.jobs.values()):
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_in_the_app_an_override_stops_applying_when_the_examples_change(live_settings):
    client = TestClient(create_app(live_settings, provider_factory=lambda s: FakeProvider()))
    results = "/results?suite=followup-email&current=v2&new=v3"
    client.post("/run", data={"suite": SUITE, "current": "v2", "new": "v3"})
    _wait(client)
    client.post("/prompts/followup-email/examples/ridgeway-pricing/override",
                data={"current": "v2", "new": "v3", "call": "other", "count_as": "worse", "reason": "Wrong tone."})
    assert "1 example overridden by you" in client.get(results).text
    client.post("/prompts/followup-email/examples/brightwater-short-call",
                data={"name": "Short call, one obvious next step", "label": "Brightwater Foods", "kind": "everyday",
                      "input": "Changed notes for this call."})
    client.post("/run", data={"suite": SUITE, "current": "v2", "new": "v3"})
    _wait(client)
    assert "overridden by you" not in client.get(results).text


# demo mode: overrides live in the signed cookie, nothing on disk --------------------------
def test_demo_override_events_live_in_the_cookie_and_removal_appends(demo_settings, no_network):
    client = TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("no calls in demo")))
    before = snapshot(SUITES)
    data = {"current": "v2", "new": "v3", "call": "not_worse", "reason": "Answered in the PS."}
    client.post("/prompts/followup-email/examples/ridgeway-pricing/override", data=data)
    client.post("/prompts/followup-email/examples/ridgeway-pricing/override/remove", data={"current": "v2", "new": "v3"})
    client.post("/prompts/followup-email/examples/ridgeway-pricing/override", data={**data, "reason": "Second look."})
    text = client.get("/results?suite=followup-email&current=v2&new=v3").text
    assert "1 example overridden by you: Second look." in text
    assert snapshot(SUITES) == before
    # Three events in the session: set, remove, set.
    import base64
    import json
    raw = client.cookies.get("rollout_ready").split(".")[0]
    session = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    assert [e.get("action", "set") for e in session["override_events"][SUITE]] == ["set", "remove", "set"]
