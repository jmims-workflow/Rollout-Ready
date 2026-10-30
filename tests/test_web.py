import time

import pytest
from fastapi.testclient import TestClient

from conftest import SUITES, FakeProvider, snapshot
from rollout_ready.web.app import create_app


@pytest.fixture
def demo(demo_settings, no_network):
    return TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("demo mode built a provider")))


@pytest.fixture
def live(live_settings):
    fake = FakeProvider()
    client = TestClient(create_app(live_settings, provider_factory=lambda s: fake))
    client.fake = fake
    return client


def wait_for_jobs(client):
    for _ in range(200):
        if all(j.finished for j in client.app.state.jobs.jobs.values()):
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_home_shows_featured_verdict(demo):
    r = demo.get("/")
    assert r.status_code == 200
    assert "Not ready to roll out" in r.text
    assert "which would have hidden both problems" in r.text
    assert "Review the 2 worse cases" in r.text
    assert "Demo mode: recorded runs, no API key" in r.text
    assert "Test a prompt change before your team sees it." in r.text
    assert 'aria-current="page">Cases' in r.text


@pytest.mark.parametrize("tab,text", [("rubric", "A 5 looks like"), ("changes", "Removed"), ("details", "Judge model")])
def test_tabs(demo, tab, text):
    r = demo.get(f"/?suite=followup-email&current=v2&new=v3&tab={tab}")
    assert r.status_code == 200 and text in r.text
    assert f'tab={tab}#cases" aria-current="page"' in r.text


def test_case_detail(demo):
    r = demo.get("/suites/followup-email/cases/ridgeway-pricing?current=v2&new=v3")
    assert r.status_code == 200
    assert "v2 4.0 to v3 2.8 (-1.2)" in r.text
    assert "The biggest drop is Answers every question asked, from 4 to 1." in r.text
    assert "never sees which version wrote it" in r.text
    assert "What a rep would paste in" in r.text


def test_summary_download(demo):
    r = demo.get("/summary.txt?suite=followup-email&current=v2&new=v3")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "Verdict: Not ready to roll out" in r.text and "Ridgeway Logistics" in r.text


def test_demo_test_page_replays_saved_example(demo):
    r = demo.get("/test")
    assert "Run the saved example" in r.text and "readonly" in r.text
    assert "If answering every question takes more, answering wins." in r.text
    r = demo.post("/test", data={"prompt_text": "anything", "note": "x"}, follow_redirects=False)
    assert r.status_code == 303
    page = demo.get(r.headers["location"])
    assert "Ready to roll out" in page.text and "saved example" in page.text


def test_all_four_verdicts_render(demo):
    for q, text in [("suite=followup-email&current=v2&new=v4", "Ready to roll out"),
                    ("suite=followup-email&current=v2&new=v3", "Not ready to roll out"),
                    ("suite=release-notes&current=v1&new=v2", "No clear difference"),
                    ("suite=release-notes&current=v2&new=v3", "Keep the current version")]:
        assert text in demo.get("/?" + q).text


def test_other_pages(demo):
    for url, text in [("/suites", "Set up a new suite"), ("/suites/followup-email", "Versions"),
                      ("/suites/new", "Draft criteria for me"), ("/scoring", "Averages hide problems")]:
        r = demo.get(url)
        assert r.status_code == 200 and text in r.text


def test_demo_mode_saves_nothing_and_makes_no_calls(demo):
    before = snapshot(SUITES)
    demo.post("/test", data={"suite": "followup-email", "against": "v2", "prompt_text": "x", "note": "y"})
    assert demo.post("/suites/followup-email/cases", data={"name": "n", "input": "i"}).status_code == 403
    assert demo.post("/suites/followup-email/cases/ridgeway-pricing", data={"name": "n", "input": "i"}).status_code == 403
    assert demo.post("/run", data={"suite": "followup-email", "current": "v2", "new": "v3"}).status_code == 403
    r = demo.post("/suites/new", data={"action": "save", "name": "X", "prompt_text": "p"})
    assert r.status_code == 403
    r = demo.post("/suites/new", data={"action": "draft"})
    assert "Names the real risk" in r.text and "Renewal risk summary" in r.text
    assert snapshot(SUITES) == before


def test_api_key_never_reaches_the_browser(live):
    for url in ["/", "/test", "/suites", "/suites/new", "/?tab=details"]:
        assert "test-key-not-real" not in live.get(url).text


def test_live_mode_runs_only_the_new_version(live, live_settings):
    r = live.post("/test", data={"suite": "followup-email", "against": "v2",
                                 "prompt_text": "A brand new prompt", "note": "Try it"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    wait_for_jobs(live)
    assert live.fake.generate_calls == 8 and live.fake.judge_calls == 8
    assert (live_settings.suites_dir / "followup-email/versions/v5.md").exists()
    page = live.get(r.headers["location"])
    assert "v5" in str(page.url) and page.status_code == 200
    assert "Live mode: runs on Claude" in page.text


def test_live_add_case_without_reload(live, live_settings):
    r = live.post("/suites/ticket-summary/cases", data={"name": "New case", "label": "Test Co", "input": "Thread"},
                  headers={"X-Requested-With": "fetch"})
    assert r.json()["ok"] and r.json()["count"] == 7


def test_live_new_suite_saves_and_scores_v1(live, live_settings):
    data = {"action": "save", "name": "Renewal risk summary", "who_uses_it": "CSMs", "prompt_text": "Summarize risk.",
            "case_name_1": "Quiet champion", "case_input_1": "Notes one", "case_name_2": "Usage drop", "case_input_2": "Notes two",
            "crit_name_1": "Names the real risk", "crit_five_1": "Clear", "crit_one_1": "Vague",
            "max_words": "100", "must_not_contain": "em dashes, synergy"}
    r = live.post("/suites/new", data=data, follow_redirects=False)
    assert r.status_code == 303
    wait_for_jobs(live)
    store = live.app.state.store
    suite = store.load_suite("renewal-risk-summary")
    assert suite.checks[1].value == [chr(0x2014), "synergy"]
    assert len(store.load_runs("renewal-risk-summary", "v1").cases) == 2


@pytest.mark.parametrize("field", ["prompt_text", "note"])
def test_twenty_thousand_character_limit(live, live_settings, field):
    data = {"suite": "followup-email", "against": "v2", "prompt_text": "ok", "note": "ok"}
    data[field] = "x" * 20_001
    r = live.post("/test", data=data)
    assert r.status_code == 400 and "The limit is 20,000" in r.text
    assert not (live_settings.suites_dir / "followup-email/versions/v5.md").exists()
    data[field] = "x" * 20_000 if field == "prompt_text" else "ok"
    assert live.post("/test", data=data, follow_redirects=False).status_code == 303


def test_limit_applies_to_test_case_inputs(live):
    r = live.post("/suites/ticket-summary/cases", data={"name": "n", "input": "y" * 20_001})
    assert r.status_code == 400 and "20,000" in r.text


def test_live_without_key_falls_back_to_demo(monkeypatch):
    from rollout_ready.config import load_settings
    monkeypatch.setenv("ROLLOUT_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = load_settings(SUITES)
    assert s.mode == "demo" and "no Anthropic API key" in s.fallback_notice
    r = TestClient(create_app(s)).get("/")
    assert "no Anthropic API key" in r.text


def test_demo_is_default_even_with_a_key(monkeypatch):
    from rollout_ready.config import load_settings
    monkeypatch.delenv("ROLLOUT_MODE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = load_settings(SUITES)
    assert s.mode == "demo" and s.api_key is None and "sk-test" not in repr(s)
