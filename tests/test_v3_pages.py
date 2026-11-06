"""PR 2a page checks: spec tests 17, 23, 24, 25 and 26 for the in-scope pages, and the help disclosures."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider
from rollout_ready.config import Limits, Settings
from rollout_ready.web.app import create_app
from test_decisions_web import form_fields
from test_web import page_text, technical_words

BANNED = ["Only what passes rolls out", "Proven on real examples",
          "can't break what already worked without you knowing", "Nothing left out"]

PAGES = ["/", "/how-it-works",
         "/i/followup-email", "/i/partner-outreach", "/i/webinar-invite",
         "/i/followup-email?tab=versions", "/i/followup-email?tab=examples", "/i/followup-email?tab=good",
         "/i/followup-email/test", "/i/partner-outreach/test", "/i/webinar-invite/test",
         "/i/followup-email/result?current=v2&new=v3", "/i/followup-email/result?current=v2&new=v4",
         "/i/partner-outreach/result?current=v2&new=v3", "/i/webinar-invite/result?current=v1&new=v2",
         "/i/webinar-invite/result?current=v2&new=v3", "/i/followup-email/tests/recorded-v1-v2",
         "/prompts/followup-email/examples/ridgeway-pricing?current=v2&new=v3", "/prompts/new"]


@pytest.fixture
def demo(demo_settings, no_network):
    return TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("no calls in demo")))


# spec test 17: required copy, and no banned phrase ------------------------------------
REQUIRED = {
    "/": ["The output reveals the gap.", "Know what a change does before your team sees it.",
          "Sound familiar?", "Instructions you manage", "Add AI instructions"],
    "/i/followup-email": ["Test history", "Version history", "Examples", "What good looks like",
                          "Last confirmed in use by the owner. Rollout Ready does not monitor changes in the external tool."],
    "/i/followup-email/test": ["Edit the instructions, then test them on 8 examples. Nothing changes for your team "
                               "until you roll it out.", "Edit the version your team uses now",
                               "What are you changing, and why?", "It will be tested on 8 examples"],
    "/i/followup-email/result?current=v2&new=v3": [
        "Not ready to roll out", "Based on 8 examples.", "What this test covers",
        "Tested on Claude using these instructions and 8 saved examples."],
    "/how-it-works": ["What each verdict means", "What rolling out does and doesn't do"],
}


@pytest.mark.parametrize("url", list(REQUIRED))
def test_each_page_has_its_required_copy(demo, url):
    text = page_text(demo.get(url).text)
    for line in REQUIRED[url]:
        assert line in text, line


@pytest.mark.parametrize("url", PAGES)
def test_no_banned_claims_on_any_page(demo, url):
    text = page_text(demo.get(url).text).lower()
    for phrase in BANNED:
        assert phrase.lower() not in text
    assert "safer" not in text and "guarantee" not in text and "proven" not in text


def test_tagline_on_every_page(demo):
    for url in ("/", "/i/followup-email", "/how-it-works"):
        assert "Test, review and roll out changes to your team's AI instructions." in page_text(demo.get(url).text)


# help in place -----------------------------------------------------------------------
def summaries(page: str) -> list[str]:
    return [page_text(s) for s in re.findall(r'<details class="whats-this"><summary>(.*?)</summary>', page)]


def test_whats_this_where_the_terms_appear(demo):
    assert summaries(demo.get("/").text) == []  # removed at review: it wasn't attached to anything
    assert summaries(demo.get("/i/followup-email").text) == [
        "What's this? the verdict", "What's this? example kinds", "What's this? deal-breakers"]
    assert summaries(demo.get("/i/followup-email/result?current=v2&new=v3").text) == [
        "What's this? example kinds", "What's this? the AI reviewer"]
    assert summaries(demo.get("/how-it-works").text) == []  # the page is the explanation


def test_help_explains_in_plain_words(demo):
    text = page_text(demo.get("/i/followup-email").text)
    assert "A deal-breaker is a quality an answer must never get wrong" in text
    assert "Caused a problem before is an input where a real answer went wrong." in text


# spec test 23: demo results are labeled as recorded --------------------------------------
@pytest.mark.parametrize("url", [u for u in PAGES if "/result" in u or "/tests/" in u])
def test_demo_results_say_recorded(demo, url):
    text = page_text(demo.get(url).text)
    assert "Saved example: recorded results" in text and "Real results" not in text


# spec test 24: session privacy on the new pages ------------------------------------------
def wait(app):
    for _ in range(300):
        if all(j.finished for j in app.state.jobs.jobs.values()):
            return
        time.sleep(0.02)


def test_one_visitors_tests_and_rollouts_stay_private(suites_copy, tmp_path):
    settings = Settings(mode="live", requested_mode="live", judge_model="claude-sonnet-5-5", suites_dir=suites_copy,
                        api_key="test-key-not-real", public=True, limits=Limits(), data_dir=tmp_path / "data")
    fake = FakeProvider()
    app = create_app(settings, provider_factory=lambda s: fake)
    a = TestClient(app, headers={"origin": "http://testserver", "x-forwarded-for": "203.0.113.5"})
    b = TestClient(app, headers={"origin": "http://testserver", "x-forwarded-for": "198.51.100.7"})
    fake.score = 3
    a.post("/run", data={"suite": "partner-outreach", "current": "v2", "new": "v2"})
    wait(app)
    fake.score = 5
    a.post("/test", data={"suite": "partner-outreach", "against": "v2", "prompt_text": "Alice's private version",
                          "note": "Alice's note"})
    wait(app)
    page = a.get("/i/partner-outreach/result?current=v2&new=v4").text
    a.post("/prompts/partner-outreach/release", data=form_fields(page, "/prompts/partner-outreach/release", "rollout"))
    mine = page_text(a.get("/i/partner-outreach").text)
    assert "In use Version 4" in mine and "Version 4 vs version 2" in mine
    card = page_text(b.get("/").text).split("Partner outreach", 1)[1].split("Webinar invite writer", 1)[0]
    assert "Alice" not in card and "Version 4" not in card  # the other visitor's Manage card
    for url in ("/i/partner-outreach", "/i/partner-outreach?tab=versions"):
        theirs = page_text(b.get(url).text)
        assert "Alice" not in theirs and "Version 4" not in theirs, url
    assert "In use Version 2" in page_text(b.get("/i/partner-outreach").text)
    assert "Version 3 tested against version 2: Needs review." in page_text(b.get("/").text)


# spec test 25: redirects from old addresses ----------------------------------------------
@pytest.mark.parametrize("old,final", [
    ("/suites", "/"), ("/prompts", "/"), ("/scoring", "/how-it-works"),
    ("/suites/followup-email", "/i/followup-email"), ("/prompts/followup-email", "/i/followup-email"),
    ("/prompts/followup-email?tab=examples", "/i/followup-email?tab=examples"),
    ("/test", "/i/followup-email/test"), ("/test?suite=webinar-invite", "/i/webinar-invite/test"),
    ("/results", "/i/followup-email/result?current=v2&new=v3"),
    ("/results?suite=webinar-invite&current=v1&new=v2", "/i/webinar-invite/result?current=v1&new=v2"),
    ("/?suite=followup-email&current=v2&new=v3", "/i/followup-email/result?current=v2&new=v3"),
    ("/prompts/followup-email/tests/recorded-v2-v4", "/i/followup-email/tests/recorded-v2-v4"),
])
def test_old_addresses_land_on_the_new_pages(demo, old, final):
    r = demo.get(old)
    assert r.status_code == 200 and str(r.url).replace("http://testserver", "") == final


# spec test 26: no technical words on any new page -------------------------------------------
@pytest.mark.parametrize("url", PAGES)
def test_plain_words_on_every_page(demo, url):
    r = demo.get(url)
    assert r.status_code == 200 and technical_words(page_text(r.text)) == []


def test_partner_outreach_says_business_development(demo):
    """Section 8 renamed the team everywhere it shows. Tested version texts are left as they were written."""
    from rollout_ready.storage import Store
    from conftest import SUITES
    suite = Store(SUITES).load_suite("partner-outreach")
    for field in (suite.description, suite.who_uses_it, suite.input_label, suite.team):
        assert "partnership" not in field.lower()
    assert "a business development rep's notes" in page_text(demo.get("/").text)
