import html
import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SUITES, FakeProvider, snapshot
from rollout_ready.web.app import create_app

FEATURED = "/results?suite=followup-email&current=v2&new=v3"
RIDGEWAY = "/prompts/followup-email/examples/ridgeway-pricing"
CALLOWAY = "/prompts/followup-email/examples/calloway-two-asks"


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


def page_text(markup: str) -> str:
    """What a person can read or hear: text, plus alt, title and aria-label values."""
    markup = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.S | re.I)
    attrs = " ".join(re.findall(r'(?:aria-label|alt|title)="([^"]*)"', markup))
    text = re.sub(r"<[^>]+>", " ", markup)
    return html.unescape(" ".join((text + " " + attrs).split()))


def override(client, url, call="not_worse", reason="The answer is there, just further down.", **extra):
    data = {"current": "v2", "new": "v3", "call": call, "reason": reason, **extra}
    return client.post(url + "/override", data=data, follow_redirects=False)


# start screen ------------------------------------------------------------------
def test_start_screen(demo):
    """Manage (spec 7a). The latest-result banner is gone (decision 1)."""
    r = demo.get("/")
    assert r.status_code == 200
    text = page_text(r.text)
    assert "The output reveals the gap. Know what a change does before your team sees it." in text
    assert ("You wrote the rule. The AI broke it anyway. Rollout Ready runs a proposed change to your team's "
            "prompts, Projects, GPTs and skills on saved examples, scores each answer against what good looks like, "
            "and shows what improved, what got worse, and what needs your judgment before you update your team's "
            "tool.") in text
    assert "New here? See an example of a change it caught" in text
    assert 'href="/results?suite=followup-email&amp;current=v2&amp;new=v3"' in r.text  # the featured Not ready result
    assert "Latest result" not in text and "See why" not in text and "verdict-icon" not in r.text
    assert "Sound familiar?" in text
    for heading, body in [
        ("It still does the thing you told it not to.",
         "Every answer is scored against your rules, so you see which ones get ignored, and on which inputs."),
        ("You fixed one thing and broke another.",
         "Every change runs on all your saved examples, so you see what a fix broke on them before your team does."),
        ("No one can say whether the change helped.",
         "You get a verdict based on the evidence, and a record of what changed, why, and who decided."),
    ]:
        assert f"{heading} {body}" in text
    assert "Instructions you manage Add AI instructions" in text and 'href="/prompts/new"' in r.text
    assert ("Claude Project Sales Sales follow-up assistant Turns a rep's discovery call notes into a follow-up email "
            "they can send the same day. In use: version 2, since October 2, 2026 Owner: Priya Raman, Sales "
            "enablement Version 4 tested against version 2: Ready to roll out.") in text
    assert "Version 3 tested against version 2: Needs review." in text
    assert "Version 3 tested against version 2: Not ready to roll out." in text
    assert "Test, review and roll out changes to your team's AI instructions." in text
    assert "Demo: sample results" in text
    assert 'class="main-nav"' in r.text  # the header nav is on every page (spec section 2)


def test_set_card_status_lines_are_neutral(demo):
    """Warning styling is for the result page only (decision 1)."""
    page = demo.get("/").text
    cards = page.split('class="plain-list set-list"', 1)[1]
    assert "verdict-" not in cards and "callout" not in cards and "alert" not in cards and "<svg" not in cards
    css = (SUITES.parent / "rollout_ready/web/static/style.css").read_text()
    rule = re.search(r"\.set-status \{([^}]*)\}", css).group(1)
    assert "alert" not in rule and "worse" not in rule


def test_new_here_link_hides_after_a_first_test(demo):
    assert "New here?" in demo.get("/").text
    demo.post("/test", data={})  # demo mode: try the saved example change
    assert "New here?" not in demo.get("/").text


def test_show_more_holds_sets_with_nothing_waiting(demo_settings, suites_copy):
    from rollout_ready.config import Settings
    settings = Settings(mode="demo", requested_mode="demo", judge_model="claude-sonnet-5-5", suites_dir=suites_copy)
    for sid in ("partner-outreach", "webinar-invite"):  # no drafts left: nothing waiting
        (suites_copy / sid / "versions" / "v3.md").unlink()
    text = page_text(TestClient(create_app(settings)).get("/").text)
    assert "Show 2 more with no changes waiting" in text
    waiting, more = text.split("Show 2 more with no changes waiting")
    assert "Sales follow-up assistant" in waiting and "Partner outreach" in more and "Webinar invite writer" in more
    assert more.count("No changes waiting.") == 2


def test_start_screen_footer_line_is_demo_only(live):
    assert "Your changes last until you close this browser" not in live.get("/").text
    assert "Live: runs on Claude" in live.get("/").text


def test_inner_pages_have_home_link_and_new_nav(demo):
    r = demo.get("/test")
    nav = re.search(r'<nav class="main-nav".*?</nav>', r.text, flags=re.S).group(0)
    assert [html.unescape(t) for t in re.findall(r">([^<]+)</a>", nav)] == ["Manage", "How it works"]
    assert 'href="/"' in nav and '<a class="brand" href="/">' in r.text


# redirects ---------------------------------------------------------------------
@pytest.mark.parametrize("old,new", [
    ("/suites", "/"),
    ("/prompts", "/"),
    ("/scoring", "/how-it-works"),
    ("/suites/followup-email", "/prompts/followup-email"),
    ("/suites/new", "/prompts/new"),
    ("/suites/followup-email/cases/ridgeway-pricing?current=v2&new=v3", RIDGEWAY + "?current=v2&new=v3"),
    ("/?suite=followup-email&current=v2&new=v3", FEATURED),
])
def test_old_urls_redirect(demo, old, new):
    r = demo.get(old, follow_redirects=False)
    assert r.status_code in (301, 308) and r.headers["location"] == new
    assert demo.get(old).status_code == 200


# results -----------------------------------------------------------------------
def test_results_opens_on_the_latest_result(demo):
    r = demo.get("/results")
    text = page_text(r.text)
    assert "Not ready to roll out" in text
    assert "Version 3 made 4 examples better and 2 examples worse." in text
    assert ('Missed a deal-breaker: "Answers every question asked" on 2 examples. '
            "A missed deal-breaker blocks a rollout, whatever the average says.") in text
    assert "Based on 8 examples. Even when a version is ready, spot-check real outputs in its first week." in text
    assert "Review the 2 examples that got worse" in text
    assert 'aria-current="page">Examples' in r.text


def test_examples_are_cards_with_worse_first(demo):
    r = demo.get(FEATURED)
    assert "<table" not in r.text.split('id="examples"')[1]
    cards = re.findall(r'<li class="example-card result-(\w+)">(.*?)</li>', r.text, flags=re.S)
    assert [c[0] for c in cards] == ["worse", "worse", "better", "better", "better", "better", "same", "same"]
    first = page_text(cards[0][1])
    assert "Pricing question left unanswered" in first and "Tricky" in first and "Ridgeway Logistics" in first
    assert "Missed a deal-breaker: Answers every question asked" in first
    assert "4.0 to 2.8" in first and "Got worse" in first
    assert "Example mix: 3 everyday, 2 messy, 2 complicated, 1 that broke before." in page_text(r.text)


@pytest.mark.parametrize("tab,text", [("good", "Deal-breaker"), ("changes", "Removed"), ("details", "AI reviewer model")])
def test_tabs(demo, tab, text):
    r = demo.get(f"{FEATURED}&tab={tab}")
    assert r.status_code == 200 and text in r.text
    assert f'tab={tab}#examples" aria-current="page"' in r.text


def test_what_changed_uses_kept_removed_added(demo):
    labels = set(re.findall(r'<span class="tag">(\w+)</span>', demo.get(f"{FEATURED}&tab=changes").text))
    assert labels == {"Kept", "Removed", "Added"}


def test_behind_the_scenes_describes_the_threshold_in_words(demo):
    text = page_text(demo.get(f"{FEATURED}&tab=details").text)
    assert "A move of half a point or more" in text


@pytest.mark.parametrize("tab", ["examples", "good", "changes", "details"])
def test_results_never_show_the_threshold_number(demo, tab):
    for q in [FEATURED, "/results?suite=webinar-invite&current=v2&new=v3"]:
        assert "0.5" not in page_text(demo.get(f"{q}&tab={tab}").text)


def test_case_detail(demo):
    r = demo.get(RIDGEWAY + "?current=v2&new=v3")
    text = page_text(r.text)
    assert r.status_code == 200
    assert "Version 2 4.0 to version 3 2.8 (-1.2)" in text
    assert "The version you're trying misses a deal-breaker: Answers every question asked, scored 1 (it was 4)." in text
    assert "never sees which version wrote it" in text
    assert "What a rep would paste in" in text
    assert "I disagree with the AI reviewer" in text
    assert "It didn't actually get worse" in text and "Something else" in text
    assert "It didn't actually get better" not in text
    assert "In this demo, your call lasts until you close your browser." in text


def test_summary_download(demo):
    r = demo.get("/summary.txt?suite=followup-email&current=v2&new=v3")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "Verdict: Not ready to roll out" in r.text and "Ridgeway Logistics" in r.text
    assert "Missed a deal-breaker" in r.text and "Based on 8 examples" in r.text
    assert "Got worse: 2, Got better: 4, About the same: 2" in r.text


def test_demo_verdicts_render(demo):
    for q, text in [("suite=followup-email&current=v2&new=v4", "Ready to roll out"),
                    ("suite=followup-email&current=v2&new=v3", "Not ready to roll out"),
                    ("suite=partner-outreach&current=v1&new=v2", "Ready to roll out"),
                    ("suite=webinar-invite&current=v1&new=v2", "No clear difference"),
                    ("suite=webinar-invite&current=v2&new=v3", "Not ready to roll out")]:
        page = demo.get("/results?" + q).text
        assert text in page and "spot-check real outputs in its first week" in page


def test_picking_a_prompt_opens_its_default_pair(demo):
    assert "Not ready to roll out" in demo.get("/results?suite=webinar-invite").text
    assert "Needs review" in demo.get("/results?suite=partner-outreach").text  # version 3 against version 2, in use


# overrides -----------------------------------------------------------------------
def test_override_changes_the_verdict_but_not_the_stored_scores(demo):
    before = snapshot(SUITES)
    r = override(demo, RIDGEWAY, reason="Pricing is answered in the PS.")
    assert r.status_code == 303 and r.headers["location"].startswith(RIDGEWAY)
    page = demo.get(FEATURED)
    text = page_text(page.text)
    assert "Not ready to roll out" in text
    assert "1 example overridden by you: Pricing is answered in the PS." in text
    assert "Overridden by you. The AI reviewer said Got worse." in text
    assert override(demo, CALLOWAY, reason="Marcus got his answer by phone.").status_code == 303
    text = page_text(demo.get(FEATURED).text)
    # Both examples now count as About the same, but their missed deal-breakers still block (spec section 5).
    assert "Got worse 0 Got better 4" in text
    assert "Not ready to roll out" in text.split("Got worse 0")[0]
    assert "2 examples overridden by you:" in text and "Marcus got his answer by phone." in text
    assert 'Missed a deal-breaker: "Answers every question asked" on 2 examples.' in text.split("Got worse 0")[0]
    summary = demo.get("/summary.txt?suite=followup-email&current=v2&new=v3").text
    assert "Verdict: Not ready to roll out" in summary and "Overridden by you:" in summary
    assert "the AI reviewer said Got worse, counted as About the same. Reason: Pricing is answered in the PS." in summary
    detail = page_text(demo.get(RIDGEWAY + "?current=v2&new=v3").text)
    assert "You overrode the AI reviewer" in detail and "Remove my override" in detail
    assert "Answers every question asked Deal-breaker 4 1" in detail  # the AI reviewer's scores are unchanged
    assert snapshot(SUITES) == before


def test_override_applies_to_that_pair_of_versions_only(demo):
    override(demo, RIDGEWAY)
    assert "overridden by you" not in demo.get("/results?suite=followup-email&current=v2&new=v4").text
    assert "overridden by you" not in demo.get("/results?suite=followup-email&current=v1&new=v3").text


def test_override_can_be_removed(demo):
    override(demo, RIDGEWAY)
    assert "1 example overridden by you" in demo.get(FEATURED).text
    r = demo.post(RIDGEWAY + "/override/remove", data={"current": "v2", "new": "v3"}, follow_redirects=False)
    assert r.status_code == 303
    text = page_text(demo.get(FEATURED).text)
    assert "overridden by you" not in text
    assert "Got worse 2" in text


def test_something_else_can_mark_an_example_worse(demo):
    r = override(demo, "/prompts/followup-email/examples/meridian-insight-demo", call="other", count_as="worse",
                 reason="It promises late-load alerts we don't have.")
    assert r.status_code == 303
    text = page_text(demo.get(FEATURED).text)
    assert "Got worse 3" in text and "Got better 4" in text and "Not ready to roll out" in text
    assert "The AI reviewer said About the same." in text


@pytest.mark.parametrize("data,message", [
    ({"call": "not_worse", "reason": ""}, "Add a one-line reason"),
    ({"call": "not_better", "reason": "x"}, "Choose what your call is"),
    ({"call": "other", "count_as": "worse", "reason": "x"}, "Choose how this example should count"),
    ({"call": "", "reason": "x"}, "Choose what your call is"),
])
def test_override_form_errors(demo, data, message):
    r = override(demo, RIDGEWAY, **data)
    assert r.status_code == 400 and message in r.text
    assert "overridden by you" not in demo.get(FEATURED).text


def test_demo_overrides_last_for_the_browser_session_only(demo, demo_settings):
    override(demo, RIDGEWAY)
    cookie = demo.cookies.get("rollout_ready")
    assert cookie and "Pricing" not in cookie
    fresh = TestClient(create_app(demo_settings))
    assert "overridden by you" not in fresh.get(FEATURED).text


def test_live_overrides_are_saved_next_to_the_results(live, live_settings):
    recorded = (live_settings.suites_dir / "followup-email/recorded/v3.json").read_text()
    live.post("/run", data={"suite": "followup-email", "current": "v2", "new": "v3"})
    wait_for_jobs(live)
    assert override(live, RIDGEWAY, call="other", count_as="worse", reason="Answered in the PS.").status_code == 303
    import json
    from rollout_ready import evaluate
    record = evaluate.latest_record(live.app.state.store, "followup-email", "v2", "v3")
    path = live_settings.suites_dir / "followup-email/runs/override-events.json"
    events = json.loads(path.read_text())
    assert [(e["fingerprint"], e["evaluation_id"], e["case_id"], e["action"]) for e in events] == [
        (record.fingerprint, record.id, "ridgeway-pricing", "set")]
    fresh = TestClient(create_app(live_settings, provider_factory=lambda s: FakeProvider()))
    assert "1 example overridden by you: Answered in the PS." in page_text(fresh.get(FEATURED).text)
    assert (live_settings.suites_dir / "followup-email/recorded/v3.json").read_text() == recorded
    live.post(RIDGEWAY + "/override/remove", data={"current": "v2", "new": "v3"})
    assert "overridden by you" not in fresh.get(FEATURED).text
    # Removing appends an event; the override stays in the history.
    assert [e["action"] for e in json.loads(path.read_text())] == ["set", "remove"]


# example mix nudge -----------------------------------------------------------------
NUDGE = "Most prompts break on messy or complicated examples. Add one or two so this test means more."


def test_no_nudge_when_the_mix_is_good(demo):
    for url in ["/prompts/followup-email", "/test", FEATURED]:
        assert NUDGE not in demo.get(url).text


@pytest.mark.parametrize("kind", ["everyday", "broke_before"])
def test_nudge_when_there_are_no_messy_or_complicated_examples(live, live_settings, kind):
    path = live_settings.suites_dir / "partner-outreach/cases.yaml"
    path.write_text(re.sub(r"kind: \w+", f"kind: {kind}", path.read_text()))
    assert NUDGE in live.get("/prompts/partner-outreach").text
    assert NUDGE in live.get("/test?suite=partner-outreach").text
    assert NUDGE not in live.get("/prompts/followup-email").text


def test_add_example_asks_for_its_kind(live):
    r = live.get("/prompts/partner-outreach")
    assert "What kind of example is this?" in r.text
    for label in ("Everyday", "Messy", "Tricky", "Caused a problem before"):
        assert label in r.text


# other pages ----------------------------------------------------------------------
def test_other_pages(demo):
    for url, text in [("/prompts", "Add AI instructions"), ("/prompts/followup-email", "Versions"),
                      ("/prompts/new", "Suggest qualities for me"), ("/how-it-works", "Why one broken example matters")]:
        r = demo.get(url)
        assert r.status_code == 200 and text in r.text


def test_prompts_show_team_tags_and_deal_breaker_badges(demo):
    text = page_text(demo.get("/prompts").text)
    for team in ("Sales", "Marketing", "Business development"):
        assert team in text
    r = demo.get("/prompts/followup-email")
    assert r.text.count('class="tag tag-deal"') == 2 and 'class="tag tag-team">Sales' in r.text


def test_new_prompt_form_has_a_deal_breaker_checkbox_per_quality(demo):
    r = demo.get("/prompts/new")
    assert r.text.count("This is a deal-breaker") == 5
    assert "Something the output must never get wrong. Most prompts have one or two." in r.text
    assert r.text.count("What kind of example is this?") == 8


def test_how_it_works_covers_every_section(demo):
    text = page_text(demo.get("/how-it-works").text)
    for heading in ("What the app does", "How the AI reviewer scores", "Why one broken example matters more than the average",
                    "Deal-breakers", "Why you can disagree with the AI reviewer", "What a good example mix looks like",
                    "How much a result proves"):
        assert heading in text
    assert "half a point or more" in text and "0.5" not in text
    assert "Six to eight good examples catch most breakages" in text


def test_demo_test_page_replays_saved_example(demo):
    r = demo.get("/test")
    assert "Run the saved example" in r.text and "readonly" in r.text
    assert "If answering every question takes more, answering wins." in r.text
    assert "Tricky" in r.text
    r = demo.post("/test", data={"prompt_text": "anything", "note": "x"}, follow_redirects=False)
    assert r.status_code == 303
    page = demo.get(r.headers["location"])
    assert "Ready to roll out" in page.text and "saved example" in page.text


def test_demo_mode_saves_nothing_and_makes_no_calls(demo):
    before = snapshot(SUITES)
    demo.post("/test", data={"suite": "followup-email", "against": "v2", "prompt_text": "x", "note": "y"})
    assert demo.post("/prompts/followup-email/examples", data={"name": "n", "input": "i"}).status_code == 403
    assert demo.post("/prompts/followup-email/examples/ridgeway-pricing", data={"name": "n", "input": "i"}).status_code == 403
    assert demo.post("/run", data={"suite": "followup-email", "current": "v2", "new": "v3"}).status_code == 403
    r = demo.post("/prompts/new", data={"action": "save", "name": "X", "prompt_text": "p"})
    assert r.status_code == 403
    r = demo.post("/prompts/new", data={"action": "draft"})
    assert "Names the real risk" in r.text and "Renewal risk summary" in r.text
    assert r.text.count('value="1" checked') == 2  # two suggested deal-breakers
    override(demo, RIDGEWAY)
    assert snapshot(SUITES) == before


# the plain words from CLAUDE.md -----------------------------------------------------------
TECHNICAL = [r"\bsuites?\b", r"\btest cases?\b", r"\btest inputs?\b", r"\brubrics?\b", r"\bcriteri(on|a)\b",
             r"\bjudge\b", r"\bautomatic checks?\b", r"\bno clear change\b", r"\bnew version\b",
             r"\bcurrent version\b", r"\bhow scoring works\b", r"\brun details\b", r"\bprompt changes\b",
             r"\bthreshold\b", r"\bevals?\b", r"\bregressions?\b", r"\bsystem prompt\b", r"\bbaseline\b"]

PAGES = ["/", "/results", f"{FEATURED}&tab=good", f"{FEATURED}&tab=changes", f"{FEATURED}&tab=details",
         "/results?suite=webinar-invite&current=v2&new=v3", "/results?suite=partner-outreach&current=v1&new=v2",
         "/results?suite=webinar-invite&current=v1&new=v2", "/results?suite=followup-email&current=v2&new=v2",
         RIDGEWAY + "?current=v2&new=v3", "/prompts/webinar-invite/examples/two-sessions-two-zones?current=v2&new=v3",
         "/test", "/prompts", "/prompts/followup-email", "/prompts/partner-outreach", "/prompts/new", "/how-it-works",
         "/prompts/nope", "/jobs/nope"]


def technical_words(text: str) -> list[str]:
    return sorted({m.group(0) for p in TECHNICAL for m in re.finditer(p, text, flags=re.I)})


@pytest.mark.parametrize("url", PAGES)
def test_no_technical_words_in_demo_pages(demo, url):
    assert technical_words(page_text(demo.get(url).text)) == []


@pytest.mark.parametrize("url", ["/", "/test", "/prompts/followup-email", "/prompts/new", FEATURED])
def test_no_technical_words_in_live_pages(live, url):
    assert technical_words(page_text(live.get(url).text)) == []


def test_no_technical_words_in_the_summary_or_demo_suggestions(demo):
    assert technical_words(demo.get("/summary.txt?suite=followup-email&current=v2&new=v3").text) == []
    assert technical_words(page_text(demo.post("/prompts/new", data={"action": "draft"}).text)) == []


def test_no_technical_words_in_live_errors(live):
    r = live.post("/prompts/new", data={"action": "save", "crit_name_1": "Only a name"})
    assert r.status_code == 400 and technical_words(page_text(r.text)) == []
    r = live.post("/prompts/partner-outreach/examples", data={"name": "n", "input": "y" * 20_001})
    assert technical_words(page_text(r.text)) == []


# live mode --------------------------------------------------------------------
def test_api_key_never_reaches_the_browser(live):
    for url in ["/", "/results", "/test", "/prompts", "/prompts/new", "/results?tab=details"]:
        assert "test-key-not-real" not in live.get(url).text


def test_live_mode_runs_only_what_has_no_real_results(live, live_settings):
    r = live.post("/test", data={"suite": "followup-email", "against": "v2",
                                 "prompt_text": "A brand new prompt", "note": "Try it"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    wait_for_jobs(live)
    # v2 has only recorded demo results, so both versions run for real.
    assert live.fake.generate_calls == 16 and live.fake.judge_calls == 16
    assert (live_settings.suites_dir / "followup-email/versions/v5.md").exists()
    page = live.get(r.headers["location"])
    assert "new=v5" in str(page.url) and page.status_code == 200
    assert "Live: runs on Claude" in page.text


def test_behind_the_scenes_says_sample_or_real_results_for_each_version(demo, live):
    text = page_text(demo.get(f"{FEATURED}&tab=details").text)
    assert "Results for version 2 Sample results" in text and "Results for version 3 Sample results" in text
    # Live mode with only recorded demo results: nothing to compare until both run for real.
    assert "Real results" not in page_text(live.get(f"{FEATURED}&tab=details").text)
    live.post("/run", data={"suite": "followup-email", "current": "v2", "new": "v3"})
    wait_for_jobs(live)
    assert live.fake.generate_calls == 16
    text = page_text(live.get(f"{FEATURED}&tab=details").text)
    assert "Results for version 2 Real results" in text and "Results for version 3 Real results" in text
    assert "Sample results" not in text


def test_test_a_change_estimate_counts_both_versions(live, live_settings):
    # v2 has only sample results, so all 8 examples run for both versions: 32 calls.
    text = page_text(live.get("/test?suite=followup-email&against=v2").text)
    assert "This will make about 32 calls to Claude" in text
    assert "It has no real results yet, so it runs on every example too." in text
    r = live.post("/prompts/followup-email/examples",
                  data={"name": "New one", "input": "Notes", "against": "v2"},
                  headers={"X-Requested-With": "fetch"})
    assert r.json()["estimate"].startswith("This will make about 36 calls to Claude")
    # Once v2 has real results, only the version you're trying runs.
    live.post("/run", data={"suite": "followup-email", "current": "v2", "new": "v2"})
    wait_for_jobs(live)
    text = page_text(live.get("/test?suite=followup-email&against=v2").text)
    assert "This will make about 18 calls to Claude" in text
    assert "only the version you're trying needs to run" in text
    calls_before = live.fake.generate_calls + live.fake.judge_calls
    live.post("/test", data={"suite": "followup-email", "against": "v2", "prompt_text": "New", "note": "Try"})
    wait_for_jobs(live)
    assert live.fake.generate_calls + live.fake.judge_calls - calls_before == 18


def test_live_start_screen_shows_the_most_recent_result(live):
    live.post("/test", data={"suite": "partner-outreach", "against": "v1", "prompt_text": "Better", "note": "Try"})
    wait_for_jobs(live)
    text = page_text(live.get("/").text)
    # The banner is gone (decision 1): the set's card says what's waiting, in neutral words.
    assert "Version 4 tested against version 1:" in text and "Latest result" not in text


def test_live_add_example_without_reload(live):
    r = live.post("/prompts/partner-outreach/examples",
                  data={"name": "New one", "label": "Test Co", "input": "Notes", "kind": "complicated"},
                  headers={"X-Requested-With": "fetch"})
    body = r.json()
    assert body["ok"] and body["count"] == 7 and body["case"]["kind"] == "complicated" and body["kind_label"] == "Tricky"


def test_live_new_prompt_saves_deal_breakers_kinds_and_team(live, live_settings):
    data = {"action": "save", "name": "Renewal risk summary", "team": "Customer success", "who_uses_it": "CSMs",
            "prompt_text": "Summarize risk.",
            "case_name_1": "Quiet champion", "case_input_1": "Notes one", "case_kind_1": "messy",
            "case_name_2": "Usage drop", "case_input_2": "Notes two",
            "crit_name_1": "Names the real risk", "crit_five_1": "Clear", "crit_one_1": "Vague", "crit_deal_1": "1",
            "crit_name_2": "Tone", "crit_five_2": "Warm", "crit_one_2": "Cold",
            "max_words": "100", "must_not_contain": "em dashes, synergy"}
    r = live.post("/prompts/new", data=data, follow_redirects=False)
    assert r.status_code == 303
    wait_for_jobs(live)
    store = live.app.state.store
    suite = store.load_suite("renewal-risk-summary")
    assert suite.team == "Customer success"
    assert [c.deal_breaker for c in suite.rubric] == [True, False]
    assert [c.kind for c in store.load_cases("renewal-risk-summary")] == ["messy", "everyday"]
    assert suite.checks[1].value == [chr(0x2014), "synergy"]
    from rollout_ready import evaluate
    record = evaluate.latest_record(store, "renewal-risk-summary", "v1", "v1")
    assert record.status == "complete" and len(record.items) == 2


def test_live_suggest_qualities_fills_the_deal_breaker_box(live):
    r = live.post("/prompts/new", data={"action": "draft", "prompt_text": "Write a summary."})
    assert r.status_code == 200 and 'value="Clear"' in r.text


@pytest.mark.parametrize("field", ["prompt_text", "note"])
def test_twenty_thousand_character_limit(live, live_settings, field):
    data = {"suite": "followup-email", "against": "v2", "prompt_text": "ok", "note": "ok"}
    data[field] = "x" * 20_001
    r = live.post("/test", data=data)
    assert r.status_code == 400 and "The limit is 20,000" in r.text
    assert not (live_settings.suites_dir / "followup-email/versions/v5.md").exists()
    data[field] = "x" * 20_000 if field == "prompt_text" else "ok"
    assert live.post("/test", data=data, follow_redirects=False).status_code == 303


def test_limit_applies_to_example_inputs(live):
    r = live.post("/prompts/partner-outreach/examples", data={"name": "n", "input": "y" * 20_001})
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


def test_add_example_estimate_counts_the_version_in_use_when_none_is_given(live):
    """Without a valid version to compare with, the estimate still counts the version in use, so it never comes in low."""
    for against in ("", "v99"):
        r = live.post("/prompts/followup-email/examples",
                      data={"name": f"New one {against}", "input": "Notes", "against": against},
                      headers={"X-Requested-With": "fetch"})
        n = r.json()["count"]
        assert r.json()["estimate"].startswith(f"This will make about {n * 2 * 2} calls to Claude")



# the demo strip ---------------------------------------------------------------------
STRIP = "This demo uses sample results for a fictional company, Northstar Systems. Your changes last until you close this browser."


def test_demo_strip_on_every_page_and_hide_is_remembered(demo):
    for url in ("/", "/how-it-works", "/prompts/followup-email"):
        assert STRIP in page_text(demo.get(url).text), url
    before = snapshot(SUITES)
    r = demo.post("/demo/hide-strip", data={"next": "/how-it-works"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/how-it-works"
    for url in ("/", "/how-it-works"):
        assert STRIP not in page_text(demo.get(url).text)
    assert snapshot(SUITES) == before


def test_hide_strip_never_redirects_off_site(demo):
    r = demo.post("/demo/hide-strip", data={"next": "//elsewhere.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_no_strip_in_local_live_mode(live):
    assert 'class="demo-strip"' not in live.get("/").text


def test_strip_contrast_and_tokens():
    css = (SUITES.parent / "rollout_ready/web/static/style.css").read_text()
    rule = re.search(r"\.demo-strip \{([^}]*)\}", css).group(1)
    assert "var(--ink)" in rule and "var(--on-primary)" in rule  # existing tokens: white on #2B1F29, 15.8 to 1
