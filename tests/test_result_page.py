"""The result page (spec 7d with PR 2a decision 2): one clear answer on top, details in tabs."""

import re

import pytest
from fastapi.testclient import TestClient

from rollout_ready.web.app import create_app
from test_web import page_text, technical_words

NOT_READY = "/i/followup-email/result?current=v2&new=v3"
READY = "/i/followup-email/result?current=v2&new=v4"
NEEDS_REVIEW = "/i/partner-outreach/result?current=v2&new=v3"
NO_DIFFERENCE = "/i/webinar-invite/result?current=v1&new=v2"


@pytest.fixture
def demo(demo_settings, no_network):
    return TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("no calls in demo")))


def above_the_fold(page: str) -> str:
    """The verdict card, without the collapsed scope disclosure."""
    card = page.split('<section class="verdict', 1)[1].split("</section>", 1)[0]
    return page_text(card.split('<details class="scope">')[0].split(">", 1)[1])


def first_tab(page: str) -> str:
    return re.search(r'<a role="tab" id="tab-(\w+)"[^>]*aria-selected="true"', page).group(1)


# above the fold: verdict, one sentence, the evidence line, the main action -----------------
def test_not_ready_shows_one_clear_answer(demo):
    top = above_the_fold(demo.get(NOT_READY).text)
    assert top == ('Not ready to roll out Version 3 made 4 examples better and 2 examples worse. The average score '
                   'went up from 3.6 to 3.9, which would have hidden both problems. Missed a deal-breaker: "Answers '
                   'every question asked" on 2 examples. Based on 8 examples. Even when a version is ready, '
                   'spot-check real outputs in its first week. Edit and test again')


def test_ready_main_action_jumps_to_the_steps_on_this_page(demo):
    page = demo.get(READY).text
    assert above_the_fold(page).endswith("Put version 4 into use")
    assert 'href="#put-into-use">Put version 4 into use</a>' in page and 'id="put-into-use"' in page
    assert "Missed a deal-breaker" not in above_the_fold(page)


def test_needs_review_main_action_opens_what_went_wrong(demo):
    page = demo.get(NEEDS_REVIEW).text
    assert above_the_fold(page).startswith("Needs review Version 3 made 3 examples better and 1 example worse.")
    assert above_the_fold(page).endswith("Review the 1 example that got worse")


def test_no_clear_difference_with_the_version_in_use(demo):
    assert above_the_fold(demo.get(NO_DIFFERENCE).text).endswith("Version 2 is the version in use.")


def test_header_details_and_scope_are_behind_disclosures(demo):
    page = demo.get(NOT_READY).text
    scope = page_text(page.split('<details class="scope">')[1].split("</details>")[0])
    assert scope == ("What this test covers Tested on Claude using these instructions and 8 saved examples. "
                     "The Project's uploaded files. Paste the facts it relies on into the instructions instead.")
    about = page_text(page.split('<details class="card about-test">')[1].split("</details>")[0])
    assert about.startswith("About this test Version 3, the version you're trying, compared with version 2, "
                            "the version your team uses now.")
    assert "Saved example: recorded results" in about and "Under 120 words." in about


def test_no_version_pickers(demo):
    page = demo.get(NOT_READY).text
    assert "<select" not in page and "Change what is shown" not in page_text(page)


# tabs: What went wrong first for Not ready and Needs review, Examples otherwise ------------
@pytest.mark.parametrize("url,tab", [(NOT_READY, "wrong"), (NEEDS_REVIEW, "wrong"), (READY, "examples"),
                                     (NO_DIFFERENCE, "examples")])
def test_first_tab(demo, url, tab):
    assert first_tab(demo.get(url).text) == tab


def test_what_went_wrong_shows_the_biggest_drop(demo):
    page = demo.get(NOT_READY).text
    panel = page_text(page.split('id="panel-wrong"')[1].split(">", 1)[1].split("</section>")[0])
    assert panel.startswith("What went wrong The biggest drop")
    assert "The biggest drop: Pricing question left unanswered (Ridgeway Logistics), 4.0 to 2.8." in panel
    assert "The input: Call with Dana Ortiz (Operations Director) and Sam Lee (IT), Ridgeway Logistics." in panel
    assert "Version 2" in panel and "Version 3" in panel
    assert "The AI reviewer on Answers every question asked: Neither question is answered" in panel
    assert "Open this example" in panel
    assert 'href="/prompts/followup-email/examples/ridgeway-pricing?current=v2&amp;new=v3"' in page


def test_what_went_wrong_is_only_for_not_ready_and_needs_review(demo):
    for url in (READY, NO_DIFFERENCE):
        page = demo.get(url).text
        assert 'id="panel-wrong"' not in page and "What went wrong" not in page_text(page)


# demo label, redirects and plain words ------------------------------------------------
def test_saved_example_is_labeled(demo):
    r = demo.post("/test", data={}, follow_redirects=True)
    assert "Saved example: recorded results, not a live test." in page_text(r.text)


@pytest.mark.parametrize("old,new", [
    ("/results?suite=followup-email&current=v2&new=v3", NOT_READY.replace("&", "&")),
    ("/results?suite=followup-email&current=v2&new=v3&tab=details", NOT_READY + "&tab=details"),
    ("/results?suite=partner-outreach", NEEDS_REVIEW),
    ("/results", NOT_READY),  # demo: the featured result
])
def test_old_results_addresses_redirect(demo, old, new):
    r = demo.get(old, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == new


@pytest.mark.parametrize("url", [NOT_READY, READY, NEEDS_REVIEW, NO_DIFFERENCE])
def test_result_pages_use_plain_words(demo, url):
    assert technical_words(page_text(demo.get(url).text)) == []
