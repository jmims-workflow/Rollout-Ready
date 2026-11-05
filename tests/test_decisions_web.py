"""Step 9: deciding in the browser. End to end in demo, local live and public live modes, spec test 12
through HTTP, and the five approved fixes to the screen description."""

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SUITES, FakeProvider, snapshot
from rollout_ready import demo_session
from rollout_ready.config import Limits, Settings
from rollout_ready.web.app import create_app
from test_web import page_text, technical_words

SUITE = "followup-email"
READY = "/results?suite=followup-email&current=v2&new=v4"
NOT_READY = "/results?suite=followup-email&current=v2&new=v3"
ORIGIN = "http://testserver"
PUBLIC_NOTE = ("Only you can see this. It lasts 24 hours or until you close this browser, whichever comes first. "
               "Putting a version into use doesn't use any of your live tests.")


def form_fields(markup: str, action: str, kind: str | None = None) -> dict:
    """The hidden fields of the form posting to action (and of this kind), as a person's browser would send."""
    for m in re.finditer(r'<form method="post" action="([^"]+)"[^>]*>(.*?)</form>', markup, flags=re.S):
        if m.group(1) != action:
            continue
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', m.group(2)))
        if kind is None or fields.get("kind") == kind:
            return fields
    raise AssertionError(f"no form for {action} {kind or ''}")


def wait(client):
    for _ in range(300):
        if all(j.finished for j in client.app.state.jobs.jobs.values()):
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def demo(demo_settings, no_network):
    return TestClient(create_app(demo_settings, provider_factory=lambda s: pytest.fail("no calls in demo")))


@pytest.fixture
def live(live_settings):
    fake = FakeProvider()
    client = TestClient(create_app(live_settings, provider_factory=lambda s: fake))
    client.fake = fake
    return client


def live_ready(client):
    """Version 2 scores 3 and the new version 5: a saved Ready to roll out record for v2 and v5."""
    client.fake.score = 3
    client.post("/run", data={"suite": SUITE, "current": "v2", "new": "v2"})
    wait(client)
    client.fake.score = 5
    client.post("/test", data={"suite": SUITE, "against": "v2", "prompt_text": "Answer every question first.",
                               "note": "Answer first"})
    wait(client)
    return "/results?suite=followup-email&current=v2&new=v5"


# demo mode, end to end --------------------------------------------------------------------
def test_demo_put_into_use_then_put_back_end_to_end(demo):
    before = snapshot(SUITES)
    page = demo.get(READY).text
    text = page_text(page)
    assert "What happens next Put version 4 into use Copy the new instructions." in text
    assert "Paste them into your team's tool: the Sales follow-up assistant project in Claude." in text
    assert "Come back and confirm, so everyone knows which version is in use." in text
    assert "Your name (optional)" in text and "Names are entered by the owner, not verified." in text
    assert "I've updated it: mark version 4 as in use" in text and "Not now" in text
    assert "Rollout Ready records your change. It doesn't update your AI tool for you." in text
    assert "In this demo, your changes last until you close this browser." in text
    assert "Results: Saved example: recorded results" in text

    fields = form_fields(page, "/prompts/followup-email/release", "rollout")
    r = demo.post("/prompts/followup-email/release", data={**fields, "name": "Priya Raman"}, follow_redirects=False)
    assert r.status_code == 303
    record = page_text(demo.get(r.headers["location"]).text)
    assert "Version 2 to version 4" in record and "Confirmed by Priya Raman" in record
    assert "What happened Put into use. Evaluation: Ready to roll out." in record
    assert "Version 4 compared with version 2 (Saved example: recorded results)" in record

    prompt = page_text(demo.get("/prompts/followup-email").text)
    assert "Version 4 In use since" in prompt and "Version 2 Retired" in prompt and "Version 3 Draft" in prompt
    assert ("Last confirmed in use by the owner. Rollout Ready does not monitor changes in the external tool."
            in prompt)

    # Put version 2 back.
    page = demo.get("/prompts/followup-email").text
    fields = form_fields(page, "/prompts/followup-email/return")  # the first form is version 1's
    fields = {**fields, "version": "v2"}
    r = demo.post("/prompts/followup-email/return", data={**fields, "reason": "Customers saw odd dates."},
                  follow_redirects=False)
    assert r.status_code == 303
    record = page_text(demo.get(r.headers["location"]).text)
    assert "Version 4 to version 2" in record
    assert "Returned to an earlier version. Reason: Customers saw odd dates." in record
    assert "No test. Putting an earlier version back doesn't rely on a test." in record
    history = page_text(demo.get("/prompts/followup-email").text).split("What's been in use")[1]
    assert history.index("Version 4 to version 2") < history.index("Version 2 to version 4")  # newest first
    assert snapshot(SUITES) == before  # nothing written to disk


def test_demo_double_submit_shows_the_same_release(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    first = demo.post("/prompts/followup-email/release", data=fields, follow_redirects=False)
    second = demo.post("/prompts/followup-email/release", data=fields, follow_redirects=False)
    assert first.headers["location"] == second.headers["location"]
    history = page_text(demo.get("/prompts/followup-email").text).split("What's been in use")[1]
    assert history.count("Version 2 to version 4") == 1


def test_demo_roll_out_anyway_on_not_ready(demo):
    page = demo.get(NOT_READY).text
    text = page_text(page)
    assert "Roll out anyway" in text and "Why roll it out anyway? (required)" in text
    fields = form_fields(page, "/prompts/followup-email/release", "exception")
    r = demo.post("/prompts/followup-email/release", data={**fields, "reason": "Sales asked for it."})
    assert r.status_code == 400
    refused = page_text(r.text)
    assert "Nothing was recorded" in refused
    assert "Tick \"I've read the 2 examples that missed a deal-breaker.\" to roll out anyway." in refused
    assert 'value="Sales asked for it."' in r.text  # what was typed is kept
    r = demo.post("/prompts/followup-email/release",
                  data={**fields, "reason": "Sales asked for it.", "deal_breaker_ack": "1"}, follow_redirects=False)
    assert r.status_code == 303
    record = page_text(demo.get(r.headers["location"]).text)
    assert ("Put into use after review. Evaluation: Not ready to roll out. Reason: Sales asked for it." in record)


def test_demo_changed_version_in_use_offers_the_right_test(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    demo.post("/prompts/followup-email/release", data=fields)
    page = demo.get(NOT_READY).text
    text = page_text(page)
    assert ("This test compared version 3 with version 2. Version 4 is in use now, so it doesn't show how "
            "version 3 compares with what your team uses.") in text
    assert "Test version 3 against version 4" in text and "/results?suite=followup-email&amp;current=v4&amp;new=v3" in page
    assert "I understand this test didn't compare against the version in use." in text
    assert "compared with version 2, the version your team uses now" not in text


def test_test_incomplete_offers_no_rollout(demo_settings, suites_copy, no_network):
    settings = Settings(mode="demo", requested_mode="demo", judge_model="claude-sonnet-5-5", suites_dir=suites_copy)
    from rollout_ready.storage import Store
    Store(suites_copy).add_version(SUITE, "Untested", "No results")
    client = TestClient(create_app(settings))
    page = client.get("/results?suite=followup-email&current=v2&new=v5").text
    text = page_text(page)
    assert "Test incomplete" in text and "Nothing can be rolled out until they're done." in text
    assert "/release" not in page and "Roll out anyway" not in text


# local live mode, end to end -----------------------------------------------------------------
def test_live_put_into_use_end_to_end(live, live_settings):
    url = live_ready(live)
    page = live.get(url).text
    assert "Results: Real results" in page_text(page)
    fields = form_fields(page, "/prompts/followup-email/release", "rollout")
    calls = live.fake.generate_calls + live.fake.judge_calls
    r = live.post("/prompts/followup-email/release", data={**fields, "name": "Priya"}, follow_redirects=False)
    assert r.status_code == 303
    assert live.fake.generate_calls + live.fake.judge_calls == calls  # rolling out makes no calls
    events = json.loads((live_settings.suites_dir / SUITE / "runs" / "releases.json").read_text())
    assert [(e["type"], e["from_version"], e["to_version"], e["evaluation_source"]) for e in events] == [
        ("rollout", "v2", "v5", "live")]
    record = page_text(live.get(r.headers["location"]).text)
    assert "Version 5 compared with version 2 (Real results)" in record
    assert "Version 5 In use" in page_text(live.get("/prompts/followup-email").text)


# spec test 12 through HTTP: a stale page is refused at submission -----------------------
def test_a_stale_page_is_refused_when_submitted(live, live_settings):
    url = live_ready(live)
    page = live.get(url).text  # the person opens the result...
    fields = form_fields(page, "/prompts/followup-email/release", "rollout")
    # ...and meanwhile an example changes.
    live.post("/prompts/followup-email/examples/ridgeway-pricing",
              data={"name": "Pricing question left unanswered", "label": "Ridgeway Logistics", "kind": "complicated",
                    "input": "New notes after the page loaded."})
    r = live.post("/prompts/followup-email/release", data=fields)
    assert r.status_code == 400
    text = page_text(r.text)
    assert "Nothing was recorded" in text
    assert ("Your examples or what good looks like changed since this version was tested. "
            "Test it again before rolling out.") in text
    assert 'data-focus-on-load' in r.text and 'role="alert"' in r.text
    assert not (live_settings.suites_dir / SUITE / "runs" / "releases.json").exists()


def test_a_quick_check_change_shows_test_again_and_it_makes_no_calls(live, live_settings):
    url = live_ready(live)
    path = live_settings.suites_dir / SUITE / "suite.yaml"
    path.write_text(path.read_text().replace("value: 150\n", "value: 140\n"))
    text = page_text(live.get(url).text)
    assert "Your examples or what good looks like changed since this version was tested." in text
    assert "Test again" in text and "What happens next" not in text
    calls = live.fake.generate_calls + live.fake.judge_calls
    live.post("/run", data={"suite": SUITE, "current": "v2", "new": "v5"})
    assert live.fake.generate_calls + live.fake.judge_calls == calls
    assert "Put version 5 into use" in page_text(live.get(url).text)


def test_results_from_separate_tests_offer_test_them_together(live):
    live.post("/run", data={"suite": SUITE, "current": "v2", "new": "v2"})
    wait(live)
    live.post("/run", data={"suite": SUITE, "current": "v3", "new": "v3"})
    wait(live)
    text = page_text(live.get(NOT_READY).text)
    assert ("These results come from separate tests, so they can't be used to roll out. Test these two versions "
            "together to get a result you can use.") in text and "Test them together" in text
    assert "What happens next" not in text
    calls = live.fake.generate_calls + live.fake.judge_calls
    live.post("/run", data={"suite": SUITE, "current": "v2", "new": "v3"})
    assert live.fake.generate_calls + live.fake.judge_calls == calls
    assert "What happens next" in page_text(live.get(NOT_READY).text)


# public live mode, end to end -----------------------------------------------------------------
def make_public(suites_copy, data_dir):
    settings = Settings(mode="live", requested_mode="live", judge_model="claude-sonnet-5-5", suites_dir=suites_copy,
                        api_key="test-key-not-real", public=True, limits=Limits(), data_dir=data_dir)
    fake = FakeProvider()
    app = create_app(settings, provider_factory=lambda s: fake)
    app.fake = fake
    return app


def visitor(app, ip="203.0.113.5"):
    client = TestClient(app, headers={"origin": ORIGIN, "x-forwarded-for": f"{ip}, 10.1.2.3"})
    client.fake = app.fake
    return client


def test_public_put_into_use_is_private_and_uses_no_live_test(suites_copy, tmp_path):
    app = make_public(suites_copy, tmp_path / "data")
    a, b = visitor(app), visitor(app, ip="198.51.100.7")
    before = snapshot(suites_copy)
    url = live_ready(a)
    page = a.get(url).text
    text = page_text(page)
    assert PUBLIC_NOTE in text and "Your name (optional)" not in text
    left_before = page_text(a.get("/test").text)
    fields = form_fields(page, "/prompts/followup-email/release", "rollout")
    r = a.post("/prompts/followup-email/release", data={**fields, "name": "Mallory"}, follow_redirects=False)
    assert r.status_code == 303
    record = page_text(a.get(r.headers["location"]).text)
    assert "Confirmed by you" in record and "Mallory" not in record
    assert "of 3 live tests left today." in left_before
    assert left_before.split("live tests left today.")[0][-8:] == \
        page_text(a.get("/test").text).split("live tests left today.")[0][-8:]  # unchanged
    assert "Version 5 In use" in page_text(a.get("/prompts/followup-email").text)
    # The other visitor sees none of it.
    other = page_text(b.get("/prompts/followup-email").text)
    assert "Version 2 In use" in other and "Version 5" not in other
    assert b.get(r.headers["location"]).status_code == 404
    assert snapshot(suites_copy) == before


def test_public_release_forms_need_our_own_pages(suites_copy, tmp_path):
    app = make_public(suites_copy, tmp_path / "data")
    stranger = TestClient(app, headers={"origin": "https://elsewhere.example"})
    r = stranger.post("/prompts/followup-email/return", data={"version": "v1", "token": "t", "reason": "x"})
    assert r.status_code == 403


# the five approved fixes --------------------------------------------------------------------
def test_fix1_retired_rows_name_the_version_they_were_tested_against(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    demo.post("/prompts/followup-email/release", data=fields)
    text = page_text(demo.get("/prompts/followup-email").text)
    assert ("Version 2 was last tested against version 1 on October 1, 2026. That test doesn't compare it with "
            "version 4, which is in use now.") in text
    assert "Version 1 was last scored on its own on September 28, 2026." in text
    assert "against version 1 on September 28" not in text and "version 1 against version 1" not in text


def test_fix2_not_ready_links_to_test_a_change(demo):
    page = demo.get(NOT_READY).text
    block = page_text(page.split('<section class="card decision"')[1].split(">", 1)[1])
    assert block.startswith("What happens next Test a change")
    assert "/test?suite=followup-email&amp;against=v2" in page and "Edit and test again" not in page_text(page)


def test_fix3_counts_read_right_for_one_and_for_more(demo, live, live_settings):
    assert "I've read the 2 examples that missed a deal-breaker." in page_text(demo.get(NOT_READY).text)
    one = page_text(demo.get("/results?suite=webinar-invite&current=v2&new=v3").text)
    assert "I've read the 1 example that missed a deal-breaker." in one
    # Needs review: one example made worse by the person's call.
    demo.post("/prompts/followup-email/examples/meridian-insight-demo/override",
              data={"current": "v2", "new": "v4", "call": "other", "count_as": "worse", "reason": "Wrong product."})
    review = page_text(demo.get(READY).text)
    assert "Needs review" in review and "Review the 1 example that got worse" in review
    # Cut off: one answer, then two.
    from rollout_ready.storage import Store
    store = Store(live_settings.suites_dir)
    for n, script in ((1, ["ok", "ok", "cut_off"]), (2, ["ok", "ok", "cut_off", "cut_off"])):
        v = store.add_version("partner-outreach", f"Prompt {n}", "Note")
        live.fake.script = list(script)
        live.post("/run", data={"suite": "partner-outreach", "current": "v1", "new": v.id})
        wait(live)
        text = page_text(live.get(f"/results?suite=partner-outreach&current=v1&new={v.id}").text)
        expected = ("1 answer was cut off at the length limit, so it wasn't scored." if n == 1 else
                    "2 answers were cut off at the length limit, so they weren't scored.")
        assert expected in text and "Retry the missing examples" in text


def test_fix4_public_note_wording(suites_copy, tmp_path):
    app = make_public(suites_copy, tmp_path / "data")
    a = visitor(app)
    # The sample comparison is shown to public visitors, with the decision block.
    text = page_text(a.get(READY).text)
    assert PUBLIC_NOTE in text


def test_fix5_rollback_links_to_the_put_back_form(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    r = demo.post("/prompts/followup-email/release", data=fields, follow_redirects=False)
    record = demo.get(r.headers["location"]).text
    assert "If something goes wrong, copy version 2, paste it back into your tool, and mark version 2 as in use." \
        in page_text(record)
    link = "/prompts/followup-email?put_back=v2#put-back-v2"
    assert f'href="{link}"' in record and "Put version 2 back into use" in page_text(record)
    prompt = demo.get(link.split("#")[0]).text
    assert '<details class="put-back" id="put-back-v2" open>' in prompt


# more states and wording ------------------------------------------------------------------
def test_interrupted_test_shows_finish_the_test(live, live_settings):
    from rollout_ready import evaluate
    from rollout_ready.storage import Store
    store = Store(live_settings.suites_dir)
    v = store.add_version(SUITE, "Interrupted version", "Note")

    class Stop(BaseException):
        pass

    class Crashes(FakeProvider):
        def generate(self, **kw):
            if self.generate_calls >= 3:
                raise Stop()
            return super().generate(**kw)
    with pytest.raises(Stop):
        evaluate.run_evaluation(store, SUITE, "v2", v.id, Crashes(), "claude-sonnet-5-5", sleep=lambda s: None)
    text = page_text(live.get(f"/results?suite=followup-email&current=v2&new={v.id}").text)
    assert "This test was interrupted: 3 of 16 finished." in text and "Finish the test" in text


def test_older_results_say_so(live, live_settings):
    import shutil
    runs = live_settings.suites_dir / SUITE / "runs"
    runs.mkdir(exist_ok=True)
    for v in ("v2", "v3"):
        shutil.copy(live_settings.suites_dir / SUITE / "recorded" / f"{v}.json", runs / f"{v}.json")
    text = page_text(live.get(NOT_READY).text)
    assert "Older result: some details weren't recorded. It can't be used to roll out." in text
    assert "Run the test again" in text


def test_no_clear_difference_wording(demo):
    url = "/results?suite=webinar-invite&current=v1&new=v2"
    # Version 2 is in use for the webinar set, so this pair rolls nothing out.
    assert "Version 2 is the version in use." in page_text(demo.get(url).text)
    # Put version 1 back: now the No clear difference result offers only Roll out anyway.
    fields = {**form_fields(demo.get("/prompts/webinar-invite").text, "/prompts/webinar-invite/return"), "version": "v1"}
    assert demo.post("/prompts/webinar-invite/return", data={**fields, "reason": "Subject lines confused people."},
                     follow_redirects=False).status_code == 303
    text = page_text(demo.get(url).text)
    assert "Nothing got clearly better or worse. Version 1 stays in use unless you roll this out anyway." in text
    assert "Roll out anyway" in text and "Put version 2 into use" not in text


def test_cookie_full_while_rolling_out_offers_start_over(demo):
    sizes = []
    refused = None
    for n in range(60):
        page = demo.get(READY if n % 2 == 0 else "/prompts/followup-email").text
        if n % 2 == 0:
            fields = form_fields(page, "/prompts/followup-email/release", "rollout")
            r = demo.post("/prompts/followup-email/release", data={**fields, "name": "P" * 60}, follow_redirects=False)
        else:
            fields = {**form_fields(page, "/prompts/followup-email/return"), "version": "v2"}
            r = demo.post("/prompts/followup-email/return", data={**fields, "reason": "r" * 199, "name": "P" * 60},
                          follow_redirects=False)
        sizes.append(len("rollout_ready=" + (demo.cookies.get("rollout_ready") or "")))
        if r.status_code == 409:
            refused = r.text
            break
        assert r.status_code == 303
    assert refused is not None and max(sizes) < demo_session.BROWSER_COOKIE_LIMIT
    text = page_text(refused)
    assert "No room for more changes" in text and "Start over" in text
    assert "This demo keeps your changes in your browser, and there's no room for more." in text


def test_new_pages_use_plain_words(demo):
    fields = form_fields(demo.get(READY).text, "/prompts/followup-email/release", "rollout")
    record = demo.post("/prompts/followup-email/release", data=fields, follow_redirects=False).headers["location"]
    for url in (READY, NOT_READY, record, "/prompts/followup-email", "/prompts/webinar-invite"):
        assert technical_words(page_text(demo.get(url).text)) == [], url
