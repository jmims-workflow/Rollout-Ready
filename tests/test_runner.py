import pytest

from conftest import FakeProvider
from rollout_ready.compare import build_comparison, missing_cases
from rollout_ready.provider import ProviderError
from rollout_ready.runner import estimate, estimate_sentence, run_version
from rollout_ready.storage import Store


def test_only_missing_cases_run(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("followup-email", "A new prompt", "Testing")
    fake = FakeProvider()
    made = run_version(store, "followup-email", v.id, fake, "claude-sonnet-5-5")
    assert made == 16 and fake.generate_calls == 8 and fake.judge_calls == 8
    # Running again reuses every saved result.
    again = FakeProvider()
    assert run_version(store, "followup-email", v.id, again, "claude-sonnet-5-5") == 0
    assert again.generate_calls == 0 and again.judge_calls == 0


def test_recorded_results_are_reused_for_the_current_version(suites_copy):
    store = Store(suites_copy)
    fake = FakeProvider()
    assert run_version(store, "followup-email", "v2", fake, "claude-sonnet-5-5") == 0
    assert fake.generate_calls == 0


def test_a_new_case_runs_only_that_case(suites_copy):
    store = Store(suites_copy)
    store.add_case("followup-email", "Brand new", "Test Co", "Call notes here")
    fake = FakeProvider()
    run_version(store, "followup-email", "v2", fake, "claude-sonnet-5-5")
    assert fake.generate_calls == 1 and fake.judge_calls == 1


def test_editing_an_input_reruns_it(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("partner-outreach", "Prompt", "Note")
    run_version(store, "partner-outreach", v.id, FakeProvider(), "j")
    store.update_case("partner-outreach", "harlow-cold", "Cold outreach", "Harlow Analytics", "Changed notes")
    suite = store.load_suite("partner-outreach")
    cases = store.load_cases("partner-outreach")
    assert [c.id for c in missing_cases(store, suite, cases, v.id, live=True)] == ["harlow-cold"]


def test_failed_call_keeps_results_already_saved(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("partner-outreach", "Prompt", "Note")
    with pytest.raises(ProviderError):
        run_version(store, "partner-outreach", v.id, FakeProvider(fail_after=5), "j")
    saved = store.load_runs("partner-outreach", v.id)
    assert len(saved.cases) == 2
    retry = FakeProvider()
    run_version(store, "partner-outreach", v.id, retry, "j")
    assert retry.generate_calls == 4  # 6 examples, 2 saved before the failure


def test_live_results_flow_into_the_same_comparison(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("partner-outreach", "Prompt", "Note")
    run_version(store, "partner-outreach", v.id, FakeProvider(score=5), "j")
    comp = build_comparison(store, "partner-outreach", "v1", v.id, live=True)
    assert comp.complete and comp.verdict == "Ready to roll out"
    assert comp.new_info.input_tokens > 0


def test_estimate_wording():
    assert estimate(8, 1) == (16, "about a minute")
    assert estimate_sentence(8, 1) == "This will make about 16 calls to Claude and take about a minute."
    assert estimate(30, 1)[1] == "about 4 minutes"
