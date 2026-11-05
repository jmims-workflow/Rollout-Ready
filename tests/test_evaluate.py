"""Evaluation records end to end with the stand-in provider (spec tests 1 to 5, 8 to 10, and the legacy part of 16)."""

import hashlib
import re
import shutil

import pytest

from conftest import FakeProvider
from rollout_ready import evaluate
from rollout_ready.compare import build_comparison, comparison_for_record
from rollout_ready.evidence import EvaluationRecord
from rollout_ready.provider import CUT_OFF_MESSAGE, Generation, JudgeReply, JudgedCriterion, Parsed, ProviderError
from rollout_ready.storage import FinalRecordError, Store

SUITE = "followup-email"
REVIEWER = "claude-sonnet-5-5"


class Waits(list):
    def __call__(self, seconds):
        self.append(seconds)


class Writer(FakeProvider):
    """Like Claude: each version writes its own answer, and a repetition writes a fresh one.

    score(answer_text, quality_id) decides each score; by default every quality gets self.score.
    """

    def __init__(self, score=4, scores=None, crash_after=None, **kw):
        super().__init__(score=score, **kw)
        self.scores = scores
        self.crash_after = crash_after
        self.seen: dict[tuple[str, str], int] = {}

    def _maybe_crash(self):
        if self.crash_after is not None and self.generate_calls + self.judge_calls >= self.crash_after:
            raise Crash()

    def generate(self, *, model, prompt, user_input):
        self._maybe_crash()
        gen = super().generate(model=model, prompt=prompt, user_input=user_input)
        n = self.seen[(prompt, user_input)] = self.seen.get((prompt, user_input), 0) + 1
        return Generation(f"{gen.text} [{tag(prompt)}] #{n}", gen.input_tokens, gen.output_tokens, gen.stop_reason)

    def structured(self, *, model, system, user, schema):
        self._maybe_crash()
        parsed = super().structured(model=model, system=system, user=user, schema=schema)
        if schema is not JudgeReply or self.scores is None or parsed.value is None:
            return parsed
        answer = re.search(r"<output_to_judge>\n(.*)\n</output_to_judge>", user, flags=re.S).group(1)
        reply = JudgeReply(scores=[JudgedCriterion(criterion_id=s.criterion_id, score=self.scores(answer, s.criterion_id),
                                                   reason=f"Scored {self.scores(answer, s.criterion_id)}.")
                                   for s in parsed.value.scores])
        return Parsed(reply, parsed.input_tokens, parsed.output_tokens)


class Crash(BaseException):
    """Stands in for the server stopping mid-test."""


def tag(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:6]


def run(store, baseline, candidate, provider, reviewer=REVIEWER, suite=SUITE):
    return evaluate.run_evaluation(store, suite, baseline, candidate, provider, reviewer, sleep=Waits())


def calls(p):
    return p.generate_calls, p.judge_calls


def edit_suite(suites_copy, old, new, suite=SUITE):
    path = suites_copy / suite / "suite.yaml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


@pytest.fixture
def store(suites_copy):
    return Store(suites_copy)


@pytest.fixture
def v5(store):
    return store.add_version(SUITE, "Write a follow-up email. Answer every question first.", "Answer first")


# requirement 1: a final record never changes ----------------------------------------
def test_storage_refuses_any_update_once_a_record_is_final(store, v5):
    rec = run(store, "v2", v5.id, Writer()).record
    path = store.records(SUITE).root / "evaluations" / f"{rec.id}.json"
    before = path.read_bytes()
    for status in ("complete", "incomplete", "interrupted"):
        with pytest.raises(FinalRecordError):
            store.finish_evaluation(rec.model_copy(update={"status": status, "verdict": None,
                                                           "finished": "2030-01-01T00:00:00Z"}))
    with pytest.raises(FinalRecordError):
        store.save_evaluation(rec)  # same id: never written twice
    assert path.read_bytes() == before


def test_the_only_update_is_running_to_final(store):
    rec = run(store, "v2", "v2", Writer()).record
    running = rec.model_copy(update={"id": "20200101T000000000000Z-test", "status": "running", "verdict": None,
                                     "finished": "", "classifications": {}, "counts": {}})
    store.save_evaluation(EvaluationRecord.model_validate(running.model_dump()))
    with pytest.raises(FinalRecordError):  # running to running is not an update
        store.finish_evaluation(EvaluationRecord.model_validate(running.model_dump()))
    store.finish_evaluation(EvaluationRecord.model_validate({**running.model_dump(), "status": "interrupted",
                                                             "finished": "2020-01-01T00:01:00Z"}))
    assert store.load_evaluation(SUITE, running.id).status == "interrupted"
    with pytest.raises(FinalRecordError):
        store.finish_evaluation(EvaluationRecord.model_validate({**running.model_dump(), "status": "incomplete",
                                                                 "finished": "2020-01-01T00:02:00Z"}))


# test 1: editing version text invalidates its answers and reviews -------------------
def test_a_version_edit_reruns_its_answers_and_reviews_and_reuses_the_rest(store, v5):
    first = Writer()
    run(store, "v2", v5.id, first)
    assert calls(first) == (16, 16)  # nothing saved yet: both versions, 8 examples
    edited = store.add_version(SUITE, v5.text + " Keep it under 120 words.", "Edited")
    p = Writer()
    run(store, "v2", edited.id, p)
    assert calls(p) == (8, 8)  # only the edited text runs; version 2 is reused
    same_text = store.add_version(SUITE, v5.text, "Same words as version 5")
    p = Writer()
    out = run(store, "v2", same_text.id, p)
    assert calls(p) == (0, 0) and out.calls == 0 and out.record.status == "complete"


# test 2: changing qualities or the reviewer reruns reviews only ---------------------
def test_a_quality_change_reruns_only_reviews(store, suites_copy, v5):
    run(store, "v2", v5.id, Writer())
    edit_suite(suites_copy, "name: Right length for a busy reader", "name: Short enough to read in a minute")
    p = Writer()
    run(store, "v2", v5.id, p)
    assert calls(p) == (0, 16)


def test_a_deal_breaker_flag_change_reruns_only_reviews(store, suites_copy, v5):
    run(store, "v2", v5.id, Writer())
    edit_suite(suites_copy, "    one_looks_like: No next step, or a vague one like \"let me know\" with no owner or date.\n",
               "    one_looks_like: No next step, or a vague one like \"let me know\" with no owner or date.\n"
               "    deal_breaker: true\n")
    p = Writer()
    run(store, "v2", v5.id, p)
    assert calls(p) == (0, 16)


def test_a_reviewer_change_reruns_only_reviews(store, v5, monkeypatch):
    run(store, "v2", v5.id, Writer())
    p = Writer()
    run(store, "v2", v5.id, p, reviewer="claude-haiku-5-5")
    assert calls(p) == (0, 16)
    from rollout_ready import evidence
    monkeypatch.setattr(evidence, "REVIEWER_PROMPT_REVISION", "2")
    p = Writer()
    run(store, "v2", v5.id, p)
    assert calls(p) == (0, 16)


# test 3: changing a quick check makes no calls ---------------------------------------
def test_a_check_change_makes_no_calls_and_recomputes_into_a_new_record(store, suites_copy, v5):
    old = run(store, "v2", v5.id, Writer()).record
    assert old.verdict == "No clear difference"  # same scores everywhere, every check passes the same way
    old_path = store.records(SUITE).root / "evaluations" / f"{old.id}.json"
    old_bytes = old_path.read_bytes()
    # A quick check the version you're trying now fails on every example, and version 2 passes.
    edit_suite(suites_copy, "    label: No em dashes\n",
               f"    label: No em dashes\n  - type: must_not_contain\n    value: [\"[{tag(v5.text)}]\"]\n"
               "    label: No leftover tags\n")
    assert evaluate.is_stale(store, old)
    p = Writer()
    out = run(store, "v2", v5.id, p)
    assert calls(p) == (0, 0) and out.calls == 0
    new = out.record
    assert new.id != old.id and new.fingerprint != old.fingerprint
    assert set(new.classifications.values()) == {"worse"} and new.verdict == "Not ready to roll out"
    candidate_items = [i for i in new.items if i.version_id == v5.id]
    assert all(any(c.label == "No leftover tags" and not c.passed for c in i.checks) for i in candidate_items)
    assert old_path.read_bytes() == old_bytes and not evaluate.is_stale(store, new)


# test 4: past records are unchanged, and each shows its own evidence ------------------
def test_past_records_stay_byte_identical_and_show_their_own_evidence(store, suites_copy, v5):
    old = run(store, "v2", v5.id, Writer()).record
    old_path = store.records(SUITE).root / "evaluations" / f"{old.id}.json"
    old_bytes = old_path.read_bytes()
    store.update_case(SUITE, "ridgeway-pricing", "Pricing question left unanswered", "Ridgeway Logistics",
                      "Completely different call notes.", "complicated")
    edit_suite(suites_copy, "name: Sounds like a person", "name: Sounds human")
    run(store, "v2", v5.id, Writer())
    assert old_path.read_bytes() == old_bytes
    then = comparison_for_record(store, SUITE, old.id)
    assert then.record.id == old.id and then.complete
    assert next(c for c in then.cases if c.id == "ridgeway-pricing").input.startswith("Call with Dana Ortiz")
    assert "Sounds like a person" in [c.name for c in then.suite.rubric]
    assert then.new.text == v5.text
    now = build_comparison(store, SUITE, "v2", v5.id, live=True)
    assert next(c for c in now.cases if c.id == "ridgeway-pricing").input == "Completely different call notes."
    assert "Sounds human" in [c.name for c in now.suite.rubric]


# test 5: a deal-breaker missed in one repetition blocks Ready -----------------------
def test_a_miss_in_one_repetition_blocks_ready_and_names_example_and_repetition(store, suites_copy, v5):
    edit_suite(suites_copy, "repeats: 1", "repeats: 2")
    ridgeway = next(c for c in store.load_cases(SUITE) if c.id == "ridgeway-pricing")

    def scores(answer, quality):
        if tag(v5.text) not in answer:
            return 3  # version 2 scores 3 on everything
        if quality == "accurate" and ridgeway.input[:40] in answer and answer.endswith("#2"):
            return 2  # the version you're trying gets one fact wrong, in its second run only
        return 5

    rec = run(store, "v2", v5.id, Writer(scores=scores)).record
    misses = [(i.case_id, m.quality_id, m.repetition, m.score) for i in rec.items for m in i.deal_breaker_misses]
    assert misses == [("ridgeway-pricing", "accurate", 2, 2)]
    comp = comparison_for_record(store, SUITE, rec.id)
    row = comp.row("ridgeway-pricing")
    assert row.criteria[0].new == 3.5  # the average across repetitions passes
    assert comp.worse == 1 and comp.better == 7  # the newly missed deal-breaker makes that example worse
    assert rec.verdict == comp.verdict == "Not ready to roll out"


# test 8: Test incomplete never produces a verdict -------------------------------------
def test_an_incomplete_test_has_no_verdict(store, v5):
    rec = run(store, "v2", v5.id, Writer(script=["ok", "ok", "cut_off"])).record
    assert rec.status == "incomplete" and rec.verdict is None and rec.classifications
    comp = build_comparison(store, SUITE, "v2", v5.id, live=True)
    assert comp.status == "incomplete" and not comp.complete and comp.verdict == "Test incomplete"
    assert comp.item_problems == [CUT_OFF_MESSAGE]


# test 9: cut-off answers, retries and failed attempts -----------------------------------
def test_a_cut_off_answer_is_an_incomplete_item_and_the_test_carries_on(store):
    v = store.add_version("partner-outreach", "Prompt", "Note")
    p = FakeProvider(script=["ok", "ok", "cut_off"])
    out = evaluate.run_evaluation(store, "partner-outreach", v.id, v.id, p, REVIEWER, sleep=Waits())
    rec = out.record
    cut = [i for i in rec.items if i.status == "cut_off"]
    assert len(cut) == 1 and cut[0].message == CUT_OFF_MESSAGE and cut[0].stop_reason == "max_tokens"
    assert not cut[0].answer_key and not cut[0].review_key  # never saved, never scored
    assert sum(i.status == "complete" for i in rec.items) == 5  # the rest carried on
    assert p.generate_attempts == 6 and p.judge_attempts == 5  # not retried, not reviewed
    assert rec.status == "incomplete" and rec.verdict is None
    retry = FakeProvider()
    evaluate.run_evaluation(store, "partner-outreach", v.id, v.id, retry, REVIEWER, sleep=Waits())
    assert calls(retry) == (1, 1)  # "Retry" calls again for the cut-off one only


def test_a_cut_off_review_keeps_the_answer_and_reruns_only_the_review(store):
    v = store.add_version("partner-outreach", "Prompt", "Note")
    p = FakeProvider(script=["ok", "cut_off"])
    rec = evaluate.run_evaluation(store, "partner-outreach", v.id, v.id, p, REVIEWER, sleep=Waits()).record
    cut = [i for i in rec.items if i.status == "cut_off"]
    assert len(cut) == 1 and cut[0].answer_key and not cut[0].review_key
    assert p.judge_attempts == 6  # one review cut off, not retried; the other five ran
    retry = FakeProvider()
    evaluate.run_evaluation(store, "partner-outreach", v.id, v.id, retry, REVIEWER, sleep=Waits())
    assert calls(retry) == (0, 1)


def test_temporary_failures_retry_twice_then_leave_the_item_incomplete(store):
    v = store.add_version("partner-outreach", "Prompt", "Note")
    busy = lambda: ProviderError("Claude is busy.", temporary=True, kind="rate_limit")  # noqa: E731
    waits = Waits()
    p = FakeProvider(script=["ok", "ok", busy(), busy(), busy()])
    with pytest.raises(ProviderError):
        evaluate.run_evaluation(store, "partner-outreach", v.id, v.id, p, REVIEWER, sleep=waits)
    rec = evaluate.latest_record(store, "partner-outreach", v.id, v.id)
    statuses = [i.status for i in rec.items]
    assert statuses == ["complete", "failed", "missing", "missing", "missing", "missing"]
    assert p.generate_attempts == 1 + 3 and len(waits) == 2
    assert rec.status == "incomplete" and rec.verdict is None


def test_failed_attempts_are_counted_but_never_scored(store):
    v = store.add_version("partner-outreach", "Prompt", "Note")
    p = FakeProvider(script=[ProviderError("Busy.", temporary=True, input_tokens=40, output_tokens=5)])
    rec = evaluate.run_evaluation(store, "partner-outreach", v.id, v.id, p, REVIEWER, sleep=Waits()).record
    first = rec.items[0]
    answer = store.load_answer("partner-outreach", first.answer_key)
    assert answer.attempts == 2 and (answer.failed_input_tokens, answer.failed_output_tokens) == (40, 5)
    assert first.input_tokens == 100 + 40 + 300 and rec.status == "complete"
    assert p.judge_calls == 6  # one review per answer that came back


# test 10: an interrupted test resumes without repeating calls ------------------------
def test_an_interrupted_test_resumes_without_repeating_calls(store, v5):
    with pytest.raises(Crash):
        run(store, "v2", v5.id, Writer(crash_after=5))  # 3 answers and 2 reviews saved, then the server stops
    rec = evaluate.latest_record(store, SUITE, "v2", v5.id)
    assert rec.status == "interrupted" and rec.verdict is None
    assert store.load_evaluation(SUITE, rec.id).status == "interrupted"  # marked once, on the next read
    finish = Writer()
    out = run(store, "v2", v5.id, finish)
    assert calls(finish) == (16 - 3, 16 - 2)
    assert out.record.status == "complete" and out.record.id != rec.id
    assert store.load_evaluation(SUITE, rec.id).status == "interrupted"  # the old one stays in history


# test 16 (part): recorded and live stay distinguishable; older results are incomplete ---
def test_recorded_and_live_records_are_labeled(store, v5):
    demo = build_comparison(store, SUITE, "v2", "v3", live=False)
    assert demo.record.source == "recorded" and demo.source_label == "Saved example: recorded results"
    assert not demo.saved and demo.current_info.label == "Sample results"
    run(store, "v2", v5.id, Writer())
    live = build_comparison(store, SUITE, "v2", v5.id, live=True)
    assert live.record.source == "live" and live.source_label == "Real results" and live.saved


def test_demo_records_are_the_same_every_time_and_write_nothing(demo_store):
    a = build_comparison(demo_store, SUITE, "v2", "v3", live=False).record
    b = build_comparison(demo_store, SUITE, "v2", "v3", live=False).record
    assert a == b and a.id.startswith("recorded-")


def test_results_saved_before_v3_show_as_incomplete_and_are_never_reused(store, suites_copy):
    for v in ("v2", "v3"):
        runs = suites_copy / SUITE / "runs"
        runs.mkdir(exist_ok=True)
        shutil.copy(suites_copy / SUITE / "recorded" / f"{v}.json", runs / f"{v}.json")
    comp = build_comparison(store, SUITE, "v2", "v3", live=True)
    assert comp.legacy and not comp.complete and comp.verdict == "Test incomplete"
    p = Writer()
    run(store, "v2", "v3", p)
    assert calls(p) == (16, 16)


# identical answers share one review ------------------------------------------------------
class SameWords(FakeProvider):
    """Two different versions that happen to write word-for-word the same answer."""

    def generate(self, *, model, prompt, user_input):
        gen = super().generate(model=model, prompt=prompt, user_input=user_input)
        return Generation(f"Reply on Friday about: {user_input[:40]}", gen.input_tokens, gen.output_tokens,
                          gen.stop_reason)


def test_identical_answer_text_from_two_versions_shares_one_review(store, v5):
    p = SameWords()
    rec = run(store, "v2", v5.id, p).record
    assert calls(p) == (16, 8)  # 16 answers, but each identical pair is reviewed once
    for case in store.load_cases(SUITE):
        old, new = (next(i for i in rec.items if i.version_id == v and i.case_id == case.id) for v in ("v2", v5.id))
        assert old.answer_key != new.answer_key  # each version keeps its own answer
        assert old.review_key == new.review_key  # the same words get the same review
    assert rec.status == "complete" and rec.verdict == "No clear difference"


# the Test a change estimate is never lower than the run ------------------------------------
def test_an_answer_not_made_yet_counts_its_review_too(store, v5):
    suite, cases = store.load_suite(SUITE), store.load_cases(SUITE)
    planned = evaluate.plan(store, suite, cases, [v5], REVIEWER)
    assert all(p.answer is None and p.calls == 2 for p in planned)
    run(store, v5.id, v5.id, Writer())
    assert all(p.calls == 0 for p in evaluate.plan(store, suite, cases, [v5], REVIEWER))


def _estimate_then_run(store, against="v2", suite=SUITE, text="A brand new version."):
    from rollout_ready.runner import change_test_estimate, estimate
    todo, calls_at_most = change_test_estimate(store, suite, against, REVIEWER)
    s = store.load_suite(suite)
    # The sentence on the page says the same number.
    assert estimate(len(store.load_cases(suite)) + todo, s.repeats)[0] == calls_at_most
    v = store.add_version(suite, text, "Try it")
    p = Writer()
    run(store, against, v.id, p, suite=suite)
    return calls_at_most, p.generate_calls + p.judge_calls


def test_change_test_estimate_is_never_lower_than_what_runs(store, suites_copy):
    # Nothing saved yet: both versions run in full.
    est, made = _estimate_then_run(store, text="First try.")
    assert est == made == 32
    # The compared version is saved: only the new one runs.
    est, made = _estimate_then_run(store, text="Second try.")
    assert est == made == 16
    # A quality changed: the compared version keeps its answers, but every review reruns.
    edit_suite(suites_copy, "name: Sounds like a person", "name: Sounds human")
    est, made = _estimate_then_run(store, text="Third try.")
    assert made == 16 + 8 and est >= made
    # A new example: one more for each version.
    store.add_case(SUITE, "Brand new", "Test Co", "Fresh notes")
    est, made = _estimate_then_run(store, text="Fourth try.")
    assert est == made == 18 + 2
    # Two runs per example, with the compared version only half saved.
    edit_suite(suites_copy, "repeats: 1", "repeats: 2")
    est, made = _estimate_then_run(store, text="Fifth try.")
    # At most: the new version in full (9 examples, 2 runs, answer and review) plus the compared
    # version's second runs (9 answers and up to 9 reviews). The estimate counts the compared
    # version in full, so it is higher, never lower.
    assert est == (9 + 9) * 2 * 2 and 9 * 2 * 2 + 9 <= made <= 9 * 2 * 2 + 9 * 2 and est >= made
