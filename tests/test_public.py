"""Public live mode (ROLLOUT_PUBLIC=true): privacy between visitors, limits, spam trap and Origin check."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider, snapshot
from rollout_ready.config import Limits, Settings
from rollout_ready.public import SESSION_SECONDS, Usage
from rollout_ready.web.app import create_app

FEATURED = "/results?suite=followup-email&current=v2&new=v3"
USED_UP = "Today's live tests are used up. Try again tomorrow, or explore the sample results."
PRIVATE = ("Your tests are private to you and last until you close this browser or for 24 hours. "
           "Don't paste anything confidential.")
ORIGIN = "http://testserver"


def make_app(suites_copy, data_dir, limits=Limits(), public=True):
    settings = Settings(mode="live", requested_mode="live", judge_model="claude-sonnet-5-5",
                        suites_dir=suites_copy, api_key="test-key-not-real", public=public,
                        limits=limits, data_dir=data_dir)
    fake = FakeProvider()
    app = create_app(settings, provider_factory=lambda s: fake)
    app.fake = fake
    return app


def visitor(app, ip="203.0.113.5"):
    return TestClient(app, headers={"origin": ORIGIN, "x-forwarded-for": f"{ip}, 10.1.2.3"})


def wait(app):
    for _ in range(300):
        if all(j.finished for j in app.state.jobs.jobs.values()):
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def calls(app):
    return app.fake.generate_calls + app.fake.judge_calls


def run_test(client, suite="followup-email", against="v2", text="Write a short follow-up email.", note="Shorter", **extra):
    r = client.post("/test", data={"suite": suite, "against": against, "prompt_text": text, "note": note, **extra},
                    follow_redirects=False)
    wait(client.app)
    return r


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def app(suites_copy, data_dir):
    return make_app(suites_copy, data_dir)


# what visitors see ---------------------------------------------------------------
def test_sample_results_stay_visible(app):
    v = visitor(app)
    home = v.get("/").text
    assert "Live: runs on Claude" in home
    assert "Latest result: version 3 of the discovery follow-up email is not ready to roll out." in home
    r = v.get(FEATURED + "&tab=details")
    assert r.status_code == 200 and "Not ready to roll out" in r.text
    assert r.text.count("Sample results") >= 2 and "Real results" not in r.text
    assert calls(app) == 0


def test_test_page_is_prefilled_and_shows_privacy_and_runs_left(app, suites_copy):
    r = visitor(app).get("/test")
    v4 = (suites_copy / "followup-email" / "versions" / "v4.md").read_text().split("---\n", 2)[2].strip()
    assert v4.splitlines()[0] in r.text
    assert "Edit this or paste your own, then run it." in r.text
    assert PRIVATE.replace("'", "&#39;") in r.text
    assert "3 of 3 live tests left today." in r.text
    assert "Up to 5,000 characters." in r.text
    assert 'name="website"' in r.text and 'aria-hidden="true"' in r.text
    prompts = visitor(app).get("/prompts").text
    assert PRIVATE.replace("'", "&#39;") in prompts


def test_a_test_runs_both_versions_for_real(app):
    v = visitor(app)
    r = run_test(v)
    assert r.status_code == 303
    done = v.get(r.headers["location"], follow_redirects=False).headers["location"]
    page = v.get(done + "&tab=details").text
    assert page.count("Real results") == 2 and "Sample results" not in page
    assert app.fake.generate_calls == 16  # 8 examples, both versions
    assert "2 of 3 live tests left today." in v.get("/test").text


# privacy ------------------------------------------------------------------------
def test_one_session_never_sees_another(app, suites_copy, data_dir):
    before = snapshot(suites_copy)
    a, b = visitor(app), visitor(app, ip="198.51.100.7")
    r = run_test(a, text="Secret pricing plan for Ridgeway.", note="Alice's change")
    job_url = r.headers["location"]
    done = a.get(job_url, follow_redirects=False).headers["location"]
    assert "new=v5" in done and "Alice&#39;s change" in a.get("/prompts/followup-email").text
    a.post("/prompts/followup-email/examples/ridgeway-pricing/override",
           data={"current": "v2", "new": "v5", "call": "not_worse", "reason": "Alice's reason"})

    # The other visitor sees none of it: not the version, the results, the override or the run.
    assert "Alice" not in b.get("/prompts/followup-email").text
    assert "Version 5" not in b.get("/prompts").text
    assert "Secret pricing" not in b.get(done).text
    assert "Alice" not in b.get("/prompts/followup-email/examples/ridgeway-pricing?current=v2&new=v5").text
    assert b.get(job_url).status_code == 404
    assert b.get(job_url + "/status").status_code == 404
    assert "Secret pricing" not in b.get("/test").text

    # A new prompt is private too, even when it has the same name as a demo prompt.
    a.post("/prompts/new", data={"action": "save", "name": "Discovery follow-up email", "prompt_text": "Alice prompt",
                                 "case_name_1": "One", "case_input_1": "Alice input", "crit_name_1": "Clear",
                                 "crit_five_1": "Very clear", "crit_one_1": "Unclear"})
    wait(app)
    assert "Alice prompt" not in b.get("/prompts/followup-email").text
    assert b.get("/prompts").text.count("Discovery follow-up email") == 1
    assert a.get("/prompts").text.count("Discovery follow-up email") == 2

    # Nothing a visitor wrote lands in the shared prompts folder or the shared cache.
    assert snapshot(suites_copy) == before
    shared = "".join(p.read_text() for p in (data_dir / "shared").rglob("*.json"))
    assert "Secret pricing" not in shared and "Alice" not in shared
    assert not (data_dir / "shared" / "followup-email" / "v5.json").exists()


def test_old_sessions_expire_after_a_day(app, monkeypatch):
    v = visitor(app)
    run_test(v)
    assert "2 of 3" in v.get("/test").text
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + SESSION_SECONDS + 60)
    page = v.get("/test").text
    assert "3 of 3 live tests left today." in page
    assert "Shorter" not in v.get("/prompts/followup-email").text


# shared cache --------------------------------------------------------------------
def test_demo_results_are_shared_but_visitor_examples_are_not(app, data_dir):
    a, b = visitor(app), visitor(app, ip="198.51.100.7")
    run_test(a)
    assert app.fake.generate_calls == 16
    cached = json.loads((data_dir / "shared" / "followup-email" / "v2.json").read_text())
    assert len(cached["cases"]) == 8

    # The next visitor reuses the demo version's real results and only runs their own version.
    run_test(b, text="Another visitor's version.")
    assert app.fake.generate_calls == 24

    # An example a visitor adds runs for them, but its result is never shared.
    a.post("/prompts/webinar-invite/examples", data={"name": "Alice case", "input": "Alice's private input"})
    run_test(a, suite="webinar-invite", against="v2")
    shared = data_dir / "shared" / "webinar-invite" / "v2.json"
    entries = json.loads(shared.read_text())["cases"]
    assert len(entries) == 6 and not any("alice" in k for k in entries)
    assert "Alice" not in shared.read_text()

    # Another visitor who edits a demo example gets their own result for it, not the shared one.
    c = visitor(app, ip="192.0.2.9")
    case_id = next(iter(entries))
    c.post(f"/prompts/webinar-invite/examples/{case_id}", data={"name": "Edited", "input": "Carol's own input"})
    before = app.fake.generate_calls
    run_test(c, suite="webinar-invite", against="v2")
    assert app.fake.generate_calls == before + 6 + 1  # her version on 6, the demo version on her edited one
    assert "Carol" not in shared.read_text()


# limits --------------------------------------------------------------------------
def test_session_limit(suites_copy, data_dir):
    app = make_app(suites_copy, data_dir, Limits(per_session=1, per_ip_per_day=5, per_day=10))
    v = visitor(app)
    assert run_test(v).status_code == 303
    made = calls(app)
    r = run_test(v, text="Another try")
    assert r.status_code == 429 and USED_UP.replace("'", "&#39;") in r.text
    assert calls(app) == made
    page = v.get("/test").text
    assert "0 of 1 live test left today." in page and "data-run-button disabled" in page
    # Sample pages keep working.
    assert v.get(FEATURED).status_code == 200 and "Not ready to roll out" in v.get(FEATURED).text
    assert v.get("/").status_code == 200


def test_per_address_limit_uses_first_forwarded_address(suites_copy, data_dir):
    app = make_app(suites_copy, data_dir, Limits(per_session=5, per_ip_per_day=2, per_day=10))
    for _ in range(2):
        assert run_test(visitor(app, ip="203.0.113.5")).status_code == 303
    made = calls(app)
    assert run_test(visitor(app, ip="203.0.113.5")).status_code == 429
    assert calls(app) == made
    assert run_test(visitor(app, ip="203.0.113.6")).status_code == 303
    # The address is never stored as is.
    assert "203.0.113" not in (data_dir / "usage.json").read_text()


def test_daily_limit_across_everyone(suites_copy, data_dir):
    app = make_app(suites_copy, data_dir, Limits(per_session=5, per_ip_per_day=5, per_day=2))
    assert run_test(visitor(app, ip="192.0.2.1")).status_code == 303
    assert run_test(visitor(app, ip="192.0.2.2")).status_code == 303
    r = run_test(visitor(app, ip="192.0.2.3"))
    assert r.status_code == 429 and USED_UP.replace("'", "&#39;") in r.text


def test_suggest_qualities_counts_as_a_run(app):
    v = visitor(app)
    r = v.post("/prompts/new", data={"action": "draft", "prompt_text": "Summarise renewal risk."})
    assert r.status_code == 200 and app.fake.judge_calls == 0 and "Clear" in r.text
    assert "2 of 3 live tests left today." in v.get("/test").text


def test_missing_run_counts_and_is_capped(suites_copy, data_dir):
    app = make_app(suites_copy, data_dir, Limits(per_session=1, per_ip_per_day=5, per_day=10))
    v = visitor(app)
    r = v.post("/run", data={"suite": "partner-outreach", "current": "v1", "new": "v1"}, follow_redirects=False)
    wait(app)
    assert r.status_code == 303 and calls(app) > 0
    made = calls(app)
    r = v.post("/run", data={"suite": "webinar-invite", "current": "v1", "new": "v2"})
    assert r.status_code == 429 and calls(app) == made


def test_text_and_example_limits(app):
    v = visitor(app)
    r = run_test(v, text="x" * 5001)
    assert r.status_code == 400 and "The limit is 5,000" in r.text
    r = v.post("/prompts/followup-email/examples", data={"name": "Ninth", "input": "One too many"})
    assert r.status_code == 400 and "up to 8 examples" in r.text
    r = v.post("/prompts/webinar-invite/examples", data={"name": "Long", "input": "y" * 5001})
    assert r.status_code == 400 and "The limit is 5,000" in r.text
    assert calls(app) == 0


# spam protection -----------------------------------------------------------------
def test_honeypot_makes_no_calls(app, data_dir):
    v = visitor(app)
    r = run_test(v, website="https://spam.example")
    assert r.status_code == 303 and r.headers["location"] == "/test"
    r = v.post("/prompts/new", data={"action": "draft", "prompt_text": "Hi", "website": "spam"})
    assert r.status_code == 200
    r = v.post("/run", data={"suite": "webinar-invite", "current": "v1", "new": "v2", "website": "spam"},
               follow_redirects=False)
    assert r.status_code == 303
    assert calls(app) == 0 and not app.state.jobs.jobs
    assert "3 of 3 live tests left today." in v.get("/test").text
    assert not (data_dir / "sessions").exists() or not any((data_dir / "sessions").rglob("v5.md"))


def test_posts_must_come_from_our_own_pages(app):
    bare = TestClient(app)
    r = bare.post("/test", data={"suite": "followup-email", "against": "v2", "prompt_text": "Hi", "note": "n"})
    assert r.status_code == 403 and "own pages" in r.text
    other = TestClient(app, headers={"origin": "https://evil.example"})
    assert other.post("/run", data={"suite": "webinar-invite", "current": "v1", "new": "v2"}).status_code == 403
    referer = TestClient(app, headers={"referer": "http://testserver/test"})
    assert referer.post("/test", data={"suite": "followup-email", "against": "v2", "prompt_text": "",
                                        "note": ""}).status_code == 400
    assert calls(app) == 0
    assert bare.get("/").status_code == 200


# local live mode is unchanged ---------------------------------------------------------
def test_local_live_mode_unchanged(suites_copy, data_dir):
    app = make_app(suites_copy, data_dir, Limits(per_session=1, per_ip_per_day=1, per_day=1), public=False)
    v = TestClient(app)  # no Origin header
    page = v.get("/test").text
    assert 'name="website"' not in page and "private to you" not in page and "live tests left" not in page
    assert "Up to 20,000 characters." in page
    for i in range(3):
        r = v.post("/test", data={"suite": "followup-email", "against": "v2", "prompt_text": f"Try {i}", "note": "n"},
                   follow_redirects=False)
        assert r.status_code == 303
        wait(app)
    assert (suites_copy / "followup-email" / "versions" / "v7.md").exists()
    assert (suites_copy / "followup-email" / "runs" / "v2.json").exists()
    assert not data_dir.exists()


def test_usage_resets_daily_but_session_count_stays():
    clock = [1_800_000_000.0]
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        u = Usage(Path(d) / "usage.json", Limits(per_session=2, per_ip_per_day=1, per_day=5), "salt", lambda: clock[0])
        assert u.take("a" * 32, "1.1.1.1") and not u.take("b" * 32, "1.1.1.1")
        clock[0] += 20 * 60 * 60
        assert u.left("b" * 32, "1.1.1.1") == 1 and u.left("a" * 32, "1.1.1.1") == 1
        assert u.take("a" * 32, "2.2.2.2") and not u.take("a" * 32, "3.3.3.3")
