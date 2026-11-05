"""Release history and decisions (spec tests 11 to 14, and the return and recorded-vs-live parts of 16)."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SUITES, FakeProvider, snapshot
from rollout_ready import demo_session, evaluate, releases, scoring
from rollout_ready.compare import build_comparison, comparison_for_record
from rollout_ready.releases import Decision, ListLog, StoreLog, decide
from rollout_ready.storage import Store
from rollout_ready.web.app import create_app

SUITE = "followup-email"
REVIEWER = "claude-sonnet-5-5"


class Waits(list):
    def __call__(self, seconds):
        self.append(seconds)


def demo_comp(store, baseline, candidate, overrides=()):
    comp = build_comparison(store, SUITE, baseline, candidate, live=False)
    return comp.apply_overrides(evaluate.as_overrides({o.case_id: o for o in overrides}))


def token():
    return releases.new_token()


# the history that ships with the app --------------------------------------------------
@pytest.mark.parametrize("suite_id,in_use,since", [
    ("followup-email", "v2", "2026-10-02T16:00:00Z"),
    ("webinar-invite", "v2", "2026-10-14T16:00:00Z"),
    ("partner-outreach", "v2", "2026-10-20T16:00:00Z"),
])
def test_demo_sets_derive_the_version_in_use_from_history(demo_store, suite_id, in_use, since):
    events = releases.shipped(demo_store, suite_id)
    assert releases.in_use_for(demo_store, suite_id) == in_use
    assert releases.since(events, in_use) == since
    assert demo_store.load_suite(suite_id).in_use == ""  # no stored status: it comes from history


def test_followup_versions_are_in_use_retired_or_draft(demo_store):
    events = releases.shipped(demo_store, SUITE)
    assert [releases.status(v, events, "v2") for v in ("v1", "v2", "v3", "v4")] == [
        "Retired", "In use", "Draft", "Draft"]


def test_every_shipped_release_matches_its_evaluation_and_follows_it(demo_store):
    for sid in demo_store.suite_ids():
        for e in releases.shipped(demo_store, sid):
            pair = evaluate.parse_recorded_id(e.evaluation_id)
            assert pair and e.evaluation_source == "recorded", (sid, e.id)
            comp = build_comparison(demo_store, sid, *pair, live=False)
            assert comp.complete and comp.record.id == e.evaluation_id
            if e.type != "baseline":
                assert comp.verdict == e.verdict, (sid, e.id)
            assert comp.record.finished <= e.timestamp, (sid, e.id)  # no release before the test it relied on


def test_webinar_version_2_is_an_exception_with_its_own_verdict(demo_store):
    event = releases.shipped(demo_store, "webinar-invite")[-1]
    assert (event.type, event.from_version, event.to_version, event.verdict) == (
        "exception", "v1", "v2", "No clear difference")
    assert event.reason == "Adds a subject line so invites can go straight into the email tool. Scores held steady."
    assert event.summary == ("Put into use after review. Evaluation: No clear difference. Reason: Adds a subject "
                             "line so invites can go straight into the email tool. Scores held steady.")


def test_demo_suite_fields(demo_store):
    s = demo_store.load_suite(SUITE)
    assert (s.type, s.owner_name, s.owner_team, s.type_label) == (
        "project", "Priya Raman", "Sales enablement", "Claude Project")
    assert s.scope_line.startswith("The Project's uploaded files.")
    assert demo_store.load_suite("partner-outreach").type == "skill"
    assert demo_store.load_suite("webinar-invite").where_text == "the Webinar invite writer GPT"


def test_where_url_must_be_a_web_address(suites_copy):
    from rollout_ready.storage import SuiteError
    path = suites_copy / SUITE / "suite.yaml"
    path.write_text(path.read_text().replace("where_text:", "where_url: not a link\nwhere_text:"))
    with pytest.raises(SuiteError, match="where_url"):
        Store(suites_copy).load_suite(SUITE)


# rolling out ---------------------------------------------------------------------------
def test_ready_rolls_out_and_the_release_keeps_verdict_and_source(suites_copy):
    store = Store(suites_copy)
    log = ListLog()
    event, problems, created = decide(store, SUITE, Decision("rollout", "v4", token(), "Priya Raman"), log,
                                      demo_comp(store, "v2", "v4"))
    assert problems == [] and created
    assert (event.type, event.from_version, event.to_version) == ("rollout", "v2", "v4")
    assert event.verdict == "Ready to roll out" and event.evaluation_id == "recorded-v2-v4"
    assert event.relied_on_sample_results and event.confirmed_by == "Priya Raman"
    history = releases.history(store, SUITE, log)
    assert releases.in_use(history, ["v1", "v2", "v3", "v4"]) == "v4"
    assert releases.status("v2", history, "v4") == "Retired"


def test_a_release_stores_the_overrides_in_force_and_an_exception_keeps_its_verdict(suites_copy):
    store = Store(suites_copy)
    comp = build_comparison(store, SUITE, "v2", "v4", live=False)
    o = evaluate.override_event(comp.record.fingerprint, comp.record.id, "meridian-insight-demo", call="other",
                                result="worse", reason="Promises late-load alerts we don't have.")
    comp = demo_comp(store, "v2", "v4", [o])
    assert comp.verdict == "Needs review"
    no_reason = decide(store, SUITE, Decision("exception", "v4", token()), ListLog(), comp, [o.id])
    assert no_reason[0] is None and "Add a one-line reason" in no_reason[1][0]
    event, problems, _ = decide(store, SUITE, Decision("exception", "v4", token(), reason="Shorter emails tested well."),
                                ListLog(), comp, [o.id])
    assert problems == []
    assert event.verdict == "Needs review" and event.override_ids == [o.id]
    assert event.summary == "Put into use after review. Evaluation: Needs review. Reason: Shorter emails tested well."


def test_ready_cant_use_roll_out_anyway_and_not_ready_cant_go_straight_into_use(suites_copy):
    store = Store(suites_copy)
    _, problems, _ = decide(store, SUITE, Decision("exception", "v4", token(), reason="Why not"), ListLog(),
                            demo_comp(store, "v2", "v4"))
    assert problems == ["This version is ready to roll out, so put it into use directly."]
    _, problems, _ = decide(store, SUITE, Decision("rollout", "v3", token()), ListLog(), demo_comp(store, "v2", "v3"))
    assert problems == ['This result is "Not ready to roll out", so it can\'t go straight into use. '
                        "Use Roll out anyway with a reason."]


# test 13: exception releases keep the unfavorable verdict -------------------------------
def test_roll_out_anyway_on_not_ready_needs_the_deal_breaker_box_and_keeps_the_verdict(suites_copy):
    store = Store(suites_copy)
    comp = demo_comp(store, "v2", "v3")
    _, problems, _ = decide(store, SUITE, Decision("exception", "v3", token(), reason="Sales asked for it."),
                            ListLog(), comp)
    assert problems == ["Tick \"I've read the 2 examples that missed a deal-breaker.\" to roll out anyway."]
    event, problems, _ = decide(store, SUITE, Decision("exception", "v3", token(), reason="Sales asked for it.",
                                                       deal_breaker_ack=True), ListLog(), comp)
    assert problems == [] and event.type == "exception" and event.verdict == "Not ready to roll out"
    assert "Not ready to roll out" in event.summary and "passed" not in event.summary.lower()


def test_test_incomplete_never_rolls_out(suites_copy):
    store = Store(suites_copy)
    v = store.add_version(SUITE, "Untested", "No results")
    comp = build_comparison(store, SUITE, "v2", v.id, live=False)
    assert comp.verdict == scoring.INCOMPLETE
    for kind in ("rollout", "exception"):
        event, problems, _ = decide(store, SUITE, Decision(kind, v.id, token(), reason="Try", deal_breaker_ack=True,
                                                           baseline_ack=True), ListLog(), comp)
        assert event is None and problems == ["This test is incomplete. Retry the missing examples before rolling out."]


# test 11: a changed in-use version triggers the warning; exceptions need acknowledgment --
def test_a_changed_version_in_use_needs_acknowledgment(suites_copy):
    store = Store(suites_copy)
    log = ListLog()
    decide(store, SUITE, Decision("rollout", "v4", token()), log, demo_comp(store, "v2", "v4"))
    comp = demo_comp(store, "v2", "v3")  # tested against version 2, but version 4 is in use now
    text = ("This test compared version 3 with version 2. Version 4 is in use now, so it doesn't show how "
            "version 3 compares with what your team uses.")
    assert releases.changed_baseline_text(3, 2, 4) == text
    _, problems, _ = decide(store, SUITE, Decision("rollout", "v3", token()), log, comp)
    assert problems == [text + " Test version 3 against version 4, or roll it out anyway."]
    reasons = dict(reason="Urgent fix.", deal_breaker_ack=True)
    _, problems, _ = decide(store, SUITE, Decision("exception", "v3", token(), **reasons), log, comp)
    assert problems == ["Tick \"I understand this test didn't compare against the version in use.\" to roll out anyway."]
    event, problems, _ = decide(store, SUITE, Decision("exception", "v3", token(), baseline_ack=True, **reasons),
                                log, comp)
    assert problems == [] and event.baseline_ack and (event.from_version, event.to_version) == ("v4", "v3")
    assert comp.record.baseline.id == "v2"  # the record still says what it compared


def test_a_ready_result_against_an_old_version_needs_roll_out_anyway(suites_copy):
    store = Store(suites_copy)
    log = ListLog([releases.ReleaseEvent(id="x", type="return", from_version="v2", to_version="v1",
                                         timestamp="2026-11-01T00:00:00Z", reason="Back to basics.")])
    comp = demo_comp(store, "v2", "v4")  # Ready, but against version 2 while version 1 is in use
    _, problems, _ = decide(store, SUITE, Decision("rollout", "v4", token()), log, comp)
    assert problems[0].startswith("This test compared version 4 with version 2. Version 1 is in use now")
    event, problems, _ = decide(store, SUITE, Decision("exception", "v4", token(), reason="Checked by hand.",
                                                       baseline_ack=True), log, comp)
    assert problems == [] and event.verdict == "Ready to roll out" and event.type == "exception"


# test 12: confirmation rechecks eligibility at submission, including a stale page --------
def _live_ready(store):
    """Version 2 scores 3, the new version scores 5: Ready to roll out, saved as one record."""
    v = store.add_version(SUITE, "Answer every question, then one clear next step.", "Answer first")
    evaluate.run_evaluation(store, SUITE, "v2", "v2", FakeProvider(score=3), REVIEWER, sleep=Waits())
    rec = evaluate.run_evaluation(store, SUITE, "v2", v.id, FakeProvider(score=5), REVIEWER, sleep=Waits()).record
    assert rec.verdict == "Ready to roll out"
    return v, rec


def test_a_stale_page_is_refused_at_submission(suites_copy):
    store = Store(suites_copy)
    v, rec = _live_ready(store)
    page = comparison_for_record(store, SUITE, rec.id)  # what the person was looking at
    assert page.verdict == "Ready to roll out"
    store.update_case(SUITE, "ridgeway-pricing", "Pricing question left unanswered", "Ridgeway Logistics",
                      "New notes after the page loaded.", "complicated")
    event, problems, created = decide(store, SUITE, Decision("rollout", v.id, token()), StoreLog(store, SUITE), page)
    assert event is None and not created and problems == [releases.STALE]
    assert store.release_events(SUITE) == []  # nothing recorded


def test_a_rollout_relies_on_a_saved_record_not_loose_results(suites_copy):
    store = Store(suites_copy)
    v = store.add_version(SUITE, "Answer every question.", "Answer first")
    # Each version scored on its own: matching answers and reviews exist, but no record compares them.
    evaluate.run_evaluation(store, SUITE, "v2", "v2", FakeProvider(score=3), REVIEWER, sleep=Waits())
    evaluate.run_evaluation(store, SUITE, v.id, v.id, FakeProvider(score=5), REVIEWER, sleep=Waits())
    loose = build_comparison(store, SUITE, "v2", v.id, live=True)
    assert loose.complete and loose.verdict == "Ready to roll out" and not loose.saved
    event, problems, _ = decide(store, SUITE, Decision("rollout", v.id, token()), StoreLog(store, SUITE), loose)
    assert event is None
    assert problems == ["Test these two versions together first, so there's a saved result to rely on."]


def test_live_rollout_on_a_saved_record(suites_copy):
    store = Store(suites_copy)
    v, rec = _live_ready(store)
    event, problems, _ = decide(store, SUITE, Decision("rollout", v.id, token(), "Priya"), StoreLog(store, SUITE),
                                comparison_for_record(store, SUITE, rec.id))
    assert problems == [] and event.evaluation_id == rec.id and event.evaluation_source == "live"
    assert not event.relied_on_sample_results
    assert releases.in_use_for(store, SUITE) == v.id
    assert json.loads((suites_copy / SUITE / "runs" / "releases.json").read_text())[0]["id"] == event.id


# test 14: duplicate confirmations create one release ------------------------------------
def test_the_same_token_twice_returns_the_first_release(suites_copy):
    store = Store(suites_copy)
    v, rec = _live_ready(store)
    t = token()
    comp = comparison_for_record(store, SUITE, rec.id)
    first, _, created = decide(store, SUITE, Decision("rollout", v.id, t), StoreLog(store, SUITE), comp)
    again, problems, created_again = decide(store, SUITE, Decision("rollout", v.id, t), StoreLog(store, SUITE), comp)
    assert created and not created_again and problems == [] and again == first
    assert len(store.release_events(SUITE)) == 1


def test_simultaneous_submits_with_one_token_make_one_release(suites_copy):
    store = Store(suites_copy)
    v, rec = _live_ready(store)
    t, results, errors = token(), [], []
    comp = comparison_for_record(store, SUITE, rec.id)
    start = threading.Barrier(6)

    def submit():
        try:
            start.wait()
            results.append(decide(store, SUITE, Decision("rollout", v.id, t), StoreLog(store, SUITE), comp))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    pool = [threading.Thread(target=submit) for _ in range(6)]
    for p in pool:
        p.start()
    for p in pool:
        p.join()
    assert errors == []
    assert len(store.release_events(SUITE)) == 1
    assert sum(created for _e, _p, created in results) == 1
    assert len({e.id for e, _p, _c in results}) == 1


def test_a_form_without_a_token_is_refused(suites_copy):
    store = Store(suites_copy)
    _, problems, _ = decide(store, SUITE, Decision("rollout", "v4", ""), ListLog(), demo_comp(store, "v2", "v4"))
    assert problems == ["This form is out of date. Reload the page and try again."]


# test 16: return makes a new event, needs a reason, never rewrites history ---------------
def test_put_back_into_use_appends_a_return_and_never_rewrites_history(suites_copy):
    store = Store(suites_copy)
    shipped_before = (suites_copy / SUITE / "history.json").read_bytes()
    v, rec = _live_ready(store)
    log = StoreLog(store, SUITE)
    decide(store, SUITE, Decision("rollout", v.id, token()), log, comparison_for_record(store, SUITE, rec.id))
    after_rollout = store.release_events(SUITE)

    _, problems, _ = decide(store, SUITE, Decision("return", "v2", token()), log)
    assert problems == ["Add a one-line reason, so your team knows why you went back."]
    _, problems, _ = decide(store, SUITE, Decision("return", "v3", token(), reason="Try it"), log)
    assert problems == ["Only a version that was in use before can be put back into use."]
    event, problems, _ = decide(store, SUITE, Decision("return", "v2", token(), reason="Customers saw odd dates."), log)
    assert problems == [] and (event.type, event.from_version, event.to_version) == ("return", v.id, "v2")
    assert event.evaluation_id == "" and event.verdict is None  # it inherits no old test's eligibility
    assert releases.in_use_for(store, SUITE) == "v2"
    assert store.release_events(SUITE)[: len(after_rollout)] == after_rollout  # earlier events unchanged
    assert (suites_copy / SUITE / "history.json").read_bytes() == shipped_before
    assert [e.type for e in releases.history(store, SUITE, log)] == ["baseline", "rollout", "rollout", "return"]


def test_release_events_refuse_impossible_shapes():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        releases.ReleaseEvent(id="a", type="rollout", from_version="v1", to_version="v2", timestamp="t",
                              evaluation_id="e", verdict="Needs review")
    with pytest.raises(ValidationError):
        releases.ReleaseEvent(id="a", type="exception", from_version="v1", to_version="v2", timestamp="t",
                              evaluation_id="e", verdict="Test incomplete", reason="x")
    with pytest.raises(ValidationError):
        releases.ReleaseEvent(id="a", type="return", from_version="v2", to_version="v1", timestamp="t")


# a new prompt starts with a baseline release ---------------------------------------------
def _wait(client):
    for _ in range(300):
        if all(j.finished for j in client.app.state.jobs.jobs.values()):
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_saving_a_new_prompt_records_version_1_as_in_use(live_settings):
    client = TestClient(create_app(live_settings, provider_factory=lambda s: FakeProvider()))
    client.post("/prompts/new", data={"action": "save", "name": "Renewal risk summary", "prompt_text": "Summarize.",
                                      "case_name_1": "One", "case_input_1": "Notes", "crit_name_1": "Clear",
                                      "crit_five_1": "Very clear", "crit_one_1": "Unclear"})
    _wait(client)
    store = client.app.state.store
    [event] = releases.history(store, "renewal-risk-summary", StoreLog(store, "renewal-risk-summary"))
    assert (event.type, event.to_version, event.summary) == ("baseline", "v1", "Recorded as in use by the owner.")


# the demo cookie stays under the browser's limit ------------------------------------------
def _cookie_value(client):
    return client.cookies.get("rollout_ready") or ""


def test_demo_cookie_stays_under_the_limit_after_many_actions(demo_settings, no_network):
    client = TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("no calls in demo")))
    before = snapshot(SUITES)
    cases = [c.id for c in Store(SUITES).load_cases(SUITE)]
    reason = "Long reason. " * 15  # close to the 200-character limit
    refused = None
    for n in range(40):
        case = cases[n % len(cases)]
        url = f"/prompts/{SUITE}/examples/{case}/override"
        r = client.post(url, data={"current": "v2", "new": "v3", "call": "other", "count_as": "better" if case in (
            "ridgeway-pricing", "calloway-two-asks") else "worse", "reason": reason[:199]}, follow_redirects=False)
        assert len("rollout_ready=" + _cookie_value(client)) < demo_session.BROWSER_COOKIE_LIMIT
        if r.status_code == 409:
            refused = (n, r.text)
            break
        assert r.status_code == 303
        client.post(url + "/remove", data={"current": "v2", "new": "v3"})
        assert len("rollout_ready=" + _cookie_value(client)) < demo_session.BROWSER_COOKIE_LIMIT
    assert refused is not None and refused[0] >= 4  # several actions fit before the cap
    assert "there&#39;s no room for more" in refused[1] and "Start over" in refused[1]
    held = len(demo_session.overrides(_session(client), SUITE))
    assert client.get("/results?suite=followup-email&current=v2&new=v3").status_code == 200  # still works
    client.post("/demo/start-over")
    assert demo_session.overrides(_session(client), SUITE) == [] and held > 0
    assert snapshot(SUITES) == before  # nothing written to disk at any point


def _session(client) -> dict:
    import base64
    raw = _cookie_value(client).split(".")[0]
    return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4))) if raw else {}


def test_mixed_override_and_release_events_never_pass_the_budget():
    session: dict = {}
    added = 0
    with pytest.raises(demo_session.CookieFull):
        for n in range(200):
            o = evaluate.override_event("f" * 64, "recorded-v2-v3", f"example-{n}", call="other", result="worse",
                                        reason="x" * 200)
            demo_session.add_override(session, SUITE, o)
            e = releases.ReleaseEvent(id=f"{n:012d}", type="exception", from_version="v2", to_version="v3",
                                      timestamp="2026-11-05T10:00:00Z", confirmed_by="P" * 60,
                                      evaluation_id="recorded-v2-v3", evaluation_source="recorded",
                                      verdict="Not ready to roll out", override_ids=[o.id] * 3, reason="y" * 200,
                                      baseline_ack=True, token="t" * 16)
            demo_session.add_release(session, SUITE, e)
            added += 2
            assert demo_session.fits(session)
    assert added >= 4 and demo_session.encoded_size(session) <= demo_session.COOKIE_BUDGET
    # The short forms read back as the same events.
    assert demo_session.releases(session, SUITE)[0].verdict == "Not ready to roll out"
    assert demo_session.overrides(session, SUITE)[0].reason == "x" * 200


# a set saved before V3 -------------------------------------------------------------------
@pytest.mark.parametrize("old_in_use,expected", [("v1", "v1"), ("", "v2")])
def test_a_set_with_no_history_gets_an_in_memory_baseline_and_nothing_is_written(suites_copy, old_in_use, expected):
    from rollout_ready.models import Case, Criterion, Suite
    store = Store(suites_copy)
    suite = Suite(name="Older set", in_use=old_in_use,
                  rubric=[Criterion(id="clear", name="Clear", five_looks_like="Clear.", one_looks_like="Unclear.")])
    sid = store.create_suite(suite, [Case(id="c1", name="One", input="Notes")], "First version", note="Original")
    store.add_version(sid, "Second version", "Shorter")
    folder = suites_copy / sid
    before = snapshot(folder)
    events = releases.history(store, sid, StoreLog(store, sid))
    assert [(e.id, e.type, e.from_version, e.to_version) for e in events] == [
        (releases.LEGACY_BASELINE_ID, "baseline", "", expected)]
    assert releases.in_use_for(store, sid) == expected
    other = "v2" if expected == "v1" else "v1"
    assert releases.status(other, events, expected) == "Draft"
    assert snapshot(folder) == before  # built in memory only
    assert not (folder / "history.json").exists() and not (folder / "runs").exists()
