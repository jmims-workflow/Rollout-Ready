"""Retries and cut-off answers (part of spec test 9). No network: a stand-in provider or SDK error objects."""

import httpx
import pytest

from conftest import FakeProvider
from rollout_ready.judge import judge_output
from rollout_ready.provider import (CUT_OFF_MESSAGE, MAX_RETRIES, ClaudeProvider, Generation, ProviderError,
                                    call_with_retries)
from rollout_ready.runner import run_version
from rollout_ready import evaluate
from rollout_ready.storage import Store


def busy(**kw):
    return ProviderError("Claude is busy right now (rate limit).", temporary=True, kind="rate_limit", **kw)


class Waits(list):
    """An injected sleep that records each wait instead of waiting."""

    def __call__(self, seconds):
        self.append(seconds)


def calls_that(*steps):
    """A function that raises or returns each step in turn, counting attempts."""
    steps = list(steps)

    def fn():
        fn.attempts += 1
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step
    fn.attempts = 0
    return fn


# call_with_retries ---------------------------------------------------------------
def test_temporary_failure_retries_twice_then_succeeds():
    waits = Waits()
    fn = calls_that(busy(), busy(), "answer")
    done = call_with_retries(fn, sleep=waits)
    assert done.value == "answer" and done.attempts == 3 and fn.attempts == 3
    assert waits == [2.0, 4.0]  # increasing waits
    assert MAX_RETRIES == 2


def test_retry_after_is_honored():
    waits = Waits()
    call_with_retries(calls_that(busy(retry_after=7.0), "answer"), sleep=waits)
    assert waits == [7.0]


def test_temporary_failure_gives_up_after_two_retries_with_tokens_counted():
    waits = Waits()
    fn = calls_that(busy(input_tokens=10, output_tokens=1), busy(input_tokens=20, output_tokens=2),
                    busy(input_tokens=30, output_tokens=3), "never reached")
    with pytest.raises(ProviderError) as err:
        call_with_retries(fn, sleep=waits)
    assert fn.attempts == 3 and len(waits) == 2
    assert (err.value.input_tokens, err.value.output_tokens) == (60, 6)


@pytest.mark.parametrize("kind", ["auth", "billing", "invalid", "not_found", "refusal", "unreadable"])
def test_errors_a_person_can_fix_are_never_retried(kind):
    waits = Waits()
    fn = calls_that(ProviderError("No.", kind=kind), "never reached")
    with pytest.raises(ProviderError):
        call_with_retries(fn, sleep=waits)
    assert fn.attempts == 1 and waits == []


def test_a_cut_off_reply_is_returned_not_retried():
    waits = Waits()
    cut = Generation("Half an ans", 100, 8000, "max_tokens")
    fn = calls_that(cut, "never reached")
    done = call_with_retries(fn, sleep=waits)
    assert done.value.cut_off and fn.attempts == 1 and waits == []


# the runner -----------------------------------------------------------------------
def test_cut_off_answer_is_not_saved_scored_or_retried(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("partner-outreach", "Prompt", "Note")
    # Examples 1 and 2 are fine (answer then review), then the third answer is cut off.
    fake = FakeProvider(script=["ok", "ok", "ok", "ok", "cut_off"])
    run_version(store, "partner-outreach", v.id, fake, "j", sleep=Waits())  # no error: the test carries on
    assert fake.generate_attempts == 6 and fake.judge_attempts == 5  # no retry, no review of the cut-off answer
    record = evaluate.latest_record(store, "partner-outreach", v.id, v.id)
    assert [i.status for i in record.items].count("cut_off") == 1 and record.status == "incomplete"
    assert next(i for i in record.items if i.status == "cut_off").message == CUT_OFF_MESSAGE
    # Retry runs it again for real, instead of reusing the cut-off answer.
    again = FakeProvider()
    run_version(store, "partner-outreach", v.id, again, "j", sleep=Waits())
    assert again.generate_calls == 1


def test_temporary_failures_retry_at_most_twice_then_stop_with_saved_results_kept(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("partner-outreach", "Prompt", "Note")
    waits = Waits()
    fake = FakeProvider(script=["ok", "ok", busy(), busy(), busy(), "never used"])
    with pytest.raises(ProviderError) as err:
        run_version(store, "partner-outreach", v.id, fake, "j", sleep=waits)
    assert err.value.temporary
    assert fake.generate_attempts == 1 + 3  # one good answer, then 3 attempts (2 retries) at the second
    assert len(waits) == 2
    record = evaluate.latest_record(store, "partner-outreach", v.id, v.id)
    assert [i.status for i in record.items].count("complete") == 1


def test_a_retry_that_succeeds_counts_failed_tokens_and_scores_only_the_good_answer(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("partner-outreach", "Prompt", "Note")
    fake = FakeProvider(script=[busy(input_tokens=40, output_tokens=5)])
    run_version(store, "partner-outreach", v.id, fake, "j", sleep=Waits())
    record = evaluate.latest_record(store, "partner-outreach", v.id, v.id)
    first = store.load_answer("partner-outreach", record.items[0].answer_key)
    assert (first.input_tokens + first.failed_input_tokens, first.output_tokens + first.failed_output_tokens) == (100 + 40, 50 + 5)
    assert record.status == "complete" and fake.generate_calls == 6 and fake.judge_calls == 6


# the AI reviewer ------------------------------------------------------------------
def _review(fake, demo_store):
    suite = demo_store.load_suite("followup-email")
    case = demo_store.load_cases("followup-email")[0]
    return judge_output(fake, "j", suite, case, "An answer.", sleep=Waits())


def test_unreadable_review_retries_once_and_counts_both_attempts(demo_store):
    fake = FakeProvider(script=["unreadable"])
    scores, parsed = _review(fake, demo_store)
    assert fake.judge_attempts == 2 and len(scores) == 5
    assert (parsed.failed_input_tokens, parsed.failed_output_tokens) == (300, 20)
    assert (parsed.input_tokens, parsed.output_tokens) == (600, 100)


def test_a_review_missing_a_score_retries_once_and_counts_its_tokens(demo_store):
    fake = FakeProvider(script=["missing_score"])
    scores, parsed = _review(fake, demo_store)
    assert fake.judge_attempts == 2 and len(scores) == 5
    assert (parsed.failed_input_tokens, parsed.failed_output_tokens) == (300, 80)


def test_two_unreadable_reviews_fail_and_nothing_is_scored(demo_store):
    fake = FakeProvider(script=["unreadable", "unreadable", "never used"])
    with pytest.raises(ProviderError) as err:
        _review(fake, demo_store)
    assert fake.judge_attempts == 2 and err.value.kind == "unreadable"
    assert (err.value.input_tokens, err.value.output_tokens) == (600, 40)


def test_cut_off_review_is_not_retried(demo_store):
    fake = FakeProvider(script=["cut_off", "never used"])
    with pytest.raises(ProviderError) as err:
        _review(fake, demo_store)
    assert fake.judge_attempts == 1 and err.value.kind == "cut_off"
    assert err.value.output_tokens == 8000


def test_temporary_review_failure_retries_inside_the_reviewer(demo_store):
    fake = FakeProvider(script=[busy(), "ok"])
    scores, parsed = _review(fake, demo_store)
    assert fake.judge_attempts == 2 and parsed.attempts == 2 and len(scores) == 5


# Claude's errors, mapped without any network call -----------------------------------
REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status, headers=None):
    return cls("error", response=httpx.Response(status, headers=headers or {}, request=REQ), body=None)


@pytest.fixture
def claude():
    return ClaudeProvider("test-key-not-real")


def test_sdk_retries_are_off(claude):
    assert claude._client.max_retries == 0


def test_error_mapping(claude):
    import anthropic as a

    cases = [
        (_status_error(a.RateLimitError, 429, {"retry-after": "12"}), True, "rate_limit", 12.0),
        (_status_error(a.RateLimitError, 429), True, "rate_limit", None),
        (_status_error(a.InternalServerError, 500), True, "server", None),
        (_status_error(a.APIStatusError, 529, {"retry-after": "3"}), True, "overloaded", 3.0),
        (a.APITimeoutError(request=REQ), True, "timeout", None),
        (a.APIConnectionError(request=REQ), True, "connection", None),
        (_status_error(a.AuthenticationError, 401), False, "auth", None),
        (_status_error(a.PermissionDeniedError, 403), False, "auth", None),
        (_status_error(a.APIStatusError, 402), False, "billing", None),
        (_status_error(a.BadRequestError, 400), False, "invalid", None),
        (_status_error(a.NotFoundError, 404), False, "not_found", None),
    ]
    for exc, temporary, kind, retry_after in cases:
        err = claude._friendly(exc)
        assert (err.temporary, err.kind, err.retry_after) == (temporary, kind, retry_after), type(exc).__name__
        assert "test-key-not-real" not in str(err)


def test_cut_off_and_unreadable_replies_from_claude(claude, monkeypatch):
    from types import SimpleNamespace as NS

    usage = NS(input_tokens=11, output_tokens=8000)
    text = NS(type="text", text="Half an ans")
    monkeypatch.setattr(claude._client.messages, "create",
                        lambda **kw: NS(stop_reason="max_tokens", content=[text], usage=usage))
    gen = claude.generate(model="m", prompt="p", user_input="u")
    assert gen.cut_off and gen.text == "Half an ans"

    monkeypatch.setattr(claude._client.messages, "parse",
                        lambda **kw: NS(stop_reason="max_tokens", parsed_output=None, usage=usage))
    parsed = claude.structured(model="m", system="s", user="u", schema=object)
    assert parsed.cut_off and parsed.value is None

    monkeypatch.setattr(claude._client.messages, "parse",
                        lambda **kw: NS(stop_reason="end_turn", parsed_output=None, usage=NS(input_tokens=5, output_tokens=6)))
    with pytest.raises(ProviderError) as err:
        claude.structured(model="m", system="s", user="u", schema=object)
    assert err.value.kind == "unreadable" and not err.value.temporary
    assert (err.value.input_tokens, err.value.output_tokens) == (5, 6)
