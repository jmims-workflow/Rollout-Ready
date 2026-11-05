"""The instructions page (spec 7b): primary button, In use card, tabs, test history (spec tests 19 and 20)."""

import re

import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider
from rollout_ready.config import Settings
from rollout_ready.web.app import create_app
from test_decisions_web import form_fields
from test_web import page_text, technical_words

READY = "/results?suite=followup-email&current=v2&new=v4"


@pytest.fixture
def demo(demo_settings, no_network):
    return TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("no calls in demo")))


def primary(client, sid):
    page = client.get(f"/i/{sid}").text
    return re.search(r'<div class="row-actions row-gap">\s*<a class="btn" href="([^"]+)">([^<]+)</a>', page).groups()


def test_old_address_redirects_with_its_query(demo):
    r = demo.get("/prompts/followup-email?put_back=v1", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/i/followup-email?put_back=v1"


def test_page_shows_what_the_set_is_and_whats_in_use(demo):
    text = page_text(demo.get("/i/followup-email").text)
    assert "Claude Project Sales Sales follow-up assistant" in text
    assert "Who uses it: Account executives, right after a discovery call." in text
    assert "Owner: Priya Raman, Sales enablement" in text
    assert ("In use Version 2: Adds next-step rule Since October 2, 2026. Last confirmed in use by the owner. "
            "Rollout Ready does not monitor changes in the external tool.") in text
    assert "Copy the instructions" in text and "Show the instructions for version 2" in text
    assert "Waiting: version 4, Answer first, then stay short" in text
    assert "Version 4 tested against version 2: Ready to roll out." in text
    assert technical_words(text) == []


# spec test 19: the primary button changes with the situation --------------------------
def test_primary_button_reads_put_into_use_for_a_current_ready_result(demo):
    url, label = primary(demo, "followup-email")
    assert label == "Put version 4 into use" and url.endswith("#put-into-use")


def test_primary_button_reads_review_results_otherwise(demo):
    assert primary(demo, "partner-outreach")[1] == "Review results"  # Needs review
    assert primary(demo, "webinar-invite")[1] == "Review results"  # Not ready to roll out


def test_primary_button_reads_test_a_change_with_nothing_waiting(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    demo.post("/prompts/followup-email/release", data=fields)  # version 4 into use: nothing newer waits
    url, label = primary(demo, "followup-email")
    assert label == "Test a change" and url.startswith("/test?suite=followup-email")
    assert "Waiting:" not in page_text(demo.get("/i/followup-email").text)


# spec test 20: test history keeps the original pair after later rollouts ------------------
def test_history_keeps_the_original_pair_after_a_rollout(demo):
    before = page_text(demo.get("/i/followup-email?tab=history").text).split("Test history", 2)[2]
    assert "Version 3 vs version 2 (in use at the time)" in before
    assert "Version 4 vs version 2 (in use at the time)" in before
    assert "Version 2 vs version 1 (in use at the time)" in before
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    demo.post("/prompts/followup-email/release", data=fields)
    after = page_text(demo.get("/i/followup-email?tab=history").text).split("Test history", 2)[2]
    assert "Version 3 vs version 2 (in use at the time)" in after  # not rewritten against version 4
    row = re.search(r'Version 3 vs version 2[^<]*</span>.*?href="([^"]+)"', demo.get("/i/followup-email").text, re.S)
    opened = page_text(demo.get(row.group(1)).text)
    assert "Not ready to roll out" in opened and "version 3" in opened.lower()


def test_live_history_rows_open_their_own_saved_test(live_settings):
    fake = FakeProvider()
    client = TestClient(create_app(live_settings, provider_factory=lambda s: fake))
    client.post("/test", data={"suite": "partner-outreach", "against": "v2", "prompt_text": "Try", "note": "n"})
    for _ in range(300):
        if all(j.finished for j in client.app.state.jobs.jobs.values()):
            break
        import time
        time.sleep(0.02)
    page = client.get("/i/partner-outreach").text
    assert "Version 4 vs version 2 (in use at the time)" in page_text(page)
    link = re.search(r'href="(/i/partner-outreach/tests/[^"]+)"', page).group(1)
    assert client.get(link).status_code == 200


# tabs work without JavaScript ---------------------------------------------------------
def test_tabs_are_links_and_every_panel_is_in_the_page(demo):
    page = demo.get("/i/followup-email?tab=examples").text
    assert re.search(r'<a role="tab" id="tab-examples" href="\?tab=examples#tabs" aria-controls="panel-examples" '
                     r'aria-selected="true">', page)
    assert 'id="panel-examples" aria-labelledby="tab-examples">' in page  # shown
    assert 'id="panel-history" aria-labelledby="tab-history" hidden>' in page  # still in the page
    for name in ("Test history", "Version history", "Examples", "What good looks like"):
        assert f">{name}</a>" in page


def test_put_back_opens_the_version_history_tab(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    demo.post("/prompts/followup-email/release", data=fields)
    page = demo.get("/i/followup-email?put_back=v2").text
    assert 'id="panel-versions" aria-labelledby="tab-versions">' in page
    assert '<details class="put-back" id="put-back-v2" open>' in page
