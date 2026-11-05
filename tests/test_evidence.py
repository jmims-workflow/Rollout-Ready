"""Reuse keys, the evidence fingerprint and record shapes (key-level parts of spec tests 1, 2 and 3)."""

import hashlib
import inspect

import pytest
from pydantic import ValidationError

from rollout_ready import evidence, judge, provider
from rollout_ready.evidence import (AnswerBlob, EvaluationRecord, ExampleSnapshot, ItemResult, OverrideEvent,
                                    ReviewBlob, VersionSnapshot, answer_key, fingerprint, review_key, text_hash)
from rollout_ready.models import Case, Check, Criterion, Score

# guards: changing the prompt or wrapper without bumping its revision fails here ------
# Each revision maps to a hash of the code it covers. When you change that code, bump the
# revision constant named in the failure message and add the new hash under the new revision.
REVIEWER_PROMPT_HASHES = {"1": "282d65061a28c71d6e9046af074ed694ef0ef6b3ff15de4b5ab8e4143145eec1"}
GENERATION_WRAPPER_HASHES = {"1": "c1287a87b777fd84cdedb3b4210e9ac9981dfbf4e6393269af91a558885b5b84"}


def _source_hash(*parts: str) -> str:
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def test_reviewer_prompt_changes_bump_the_revision():
    now = _source_hash(judge.JUDGE_SYSTEM, inspect.getsource(judge.judge_prompt), inspect.getsource(judge._escape))
    expected = REVIEWER_PROMPT_HASHES.get(evidence.REVIEWER_PROMPT_REVISION)
    assert now == expected, (
        "The AI reviewer's prompt changed (JUDGE_SYSTEM, judge_prompt or _escape in rollout_ready/judge.py). "
        "Bump REVIEWER_PROMPT_REVISION in rollout_ready/evidence.py, so saved reviews are rerun, then add "
        f'the new hash "{now}" under the new revision in REVIEWER_PROMPT_HASHES in tests/test_evidence.py.')


def test_generation_wrapper_changes_bump_the_revision():
    now = _source_hash(inspect.getsource(provider.ClaudeProvider.generate))
    expected = GENERATION_WRAPPER_HASHES.get(evidence.GENERATION_WRAPPER_REVISION)
    assert now == expected, (
        "The way a version and an example are sent to Claude changed (ClaudeProvider.generate in "
        "rollout_ready/provider.py). Bump GENERATION_WRAPPER_REVISION in rollout_ready/evidence.py, so saved "
        f'answers are rerun, then add the new hash "{now}" under the new revision in GENERATION_WRAPPER_HASHES '
        "in tests/test_evidence.py.")


# shared inputs --------------------------------------------------------------------
def _rubric(**changes):
    facts = dict(id="facts", name="Facts right", five_looks_like="All correct.", one_looks_like="Made up.",
                 deal_breaker=True)
    facts.update(changes)
    return [Criterion(**facts),
            Criterion(id="tone", name="Tone", five_looks_like="Warm.", one_looks_like="Stiff.")]


ANSWER = dict(version_text="Write a short follow-up.", example_input="Call notes.", model="claude-sonnet-5-5",
              repetition=1)
REVIEW = dict(answer_hash=text_hash("Hi Dana, here is the pricing."), rubric=_rubric(),
              reviewer_model="claude-sonnet-5-5")


# test 1: editing version text invalidates its answers and reviews --------------------
def test_same_inputs_give_the_same_answer_key():
    assert answer_key(**ANSWER) == answer_key(**ANSWER)


@pytest.mark.parametrize("change", [
    {"version_text": "Write a short follow-up. Answer every question."},
    {"example_input": "Call notes, edited."},
    {"model": "claude-haiku-5-5"},
    {"repetition": 2},
    {"settings": {"max_tokens": 4000}},
    {"wrapper_revision": "2"},
])
def test_any_generation_input_change_gives_a_new_answer_key(change):
    assert answer_key(**{**ANSWER, **change}) != answer_key(**ANSWER)


def test_a_new_answer_always_gets_a_new_review_key():
    """Editing the version text means a new answer, so its review can't be reused either."""
    old = review_key(**REVIEW)
    new = review_key(**{**REVIEW, "answer_hash": text_hash("Hi Dana, here is the pricing and the date.")})
    assert new != old


def test_repetitions_are_numbered_from_one():
    with pytest.raises(ValueError):
        answer_key(**{**ANSWER, "repetition": 0})


# test 2: changing qualities or the reviewer reruns reviews only ---------------------
@pytest.mark.parametrize("change", [
    {"rubric": _rubric(name="Every fact right")},
    {"rubric": _rubric(five_looks_like="Every name, date and price is correct.")},
    {"rubric": _rubric(one_looks_like="Gets a price wrong.")},
    {"rubric": _rubric(deal_breaker=False)},
    {"rubric": _rubric()[:1]},
    {"rubric": [*_rubric(), Criterion(id="next", name="Next step", five_looks_like="a", one_looks_like="b")]},
    {"reviewer_model": "claude-haiku-5-5"},
    {"reviewer_revision": "2"},
])
def test_quality_or_reviewer_change_gives_a_new_review_key_but_keeps_the_answer(change):
    assert review_key(**{**REVIEW, **change}) != review_key(**REVIEW)
    assert answer_key(**ANSWER) == answer_key(**ANSWER)  # answer key takes none of these inputs


def test_answer_key_takes_no_quality_or_reviewer_input():
    params = set(inspect.signature(answer_key).parameters)
    assert not params & {"rubric", "qualities", "reviewer_model", "reviewer_revision", "checks"}


# test 3: changing a quick check makes no calls -------------------------------------
def test_quick_checks_are_in_neither_key():
    for fn in (answer_key, review_key):
        assert "checks" not in inspect.signature(fn).parameters


# fingerprint ------------------------------------------------------------------------
@pytest.fixture
def evidence_inputs(demo_store):
    suite = demo_store.load_suite("followup-email")
    return dict(source="live", suite=suite, cases=demo_store.load_cases("followup-email"),
                candidate_id="v3", candidate_text=demo_store.load_version("followup-email", "v3").text,
                baseline_id="v2", baseline_text=demo_store.load_version("followup-email", "v2").text,
                reviewer_model="claude-sonnet-5-5")


def _with_suite(inputs, **changes):
    return {**inputs, "suite": inputs["suite"].model_copy(update=changes)}


def test_changing_a_quick_check_changes_the_fingerprint(evidence_inputs):
    before = fingerprint(**evidence_inputs)
    checks = list(evidence_inputs["suite"].checks)
    checks[0] = Check(type="max_words", value=120, label="Under 120 words")
    assert fingerprint(**_with_suite(evidence_inputs, checks=checks)) != before
    assert fingerprint(**_with_suite(evidence_inputs, checks=checks[1:])) != before


def test_fingerprint_changes_with_every_evidence_input(evidence_inputs):
    base = fingerprint(**evidence_inputs)
    s = evidence_inputs["suite"]
    cases = evidence_inputs["cases"]
    changed = [
        {"source": "recorded"},
        {"candidate_text": evidence_inputs["candidate_text"] + " Be brief."},
        {"candidate_id": "v4"},
        {"baseline_text": "Something else."},
        {"baseline_id": "v1"},
        {"cases": cases[:-1]},
        {"cases": [cases[0].model_copy(update={"input": "Edited notes."}), *cases[1:]]},
        {"reviewer_model": "claude-haiku-5-5"},
        {"settings": {"max_tokens": 4000}},
        {"wrapper_revision": "2"},
        {"reviewer_revision": "2"},
        _with_suite(evidence_inputs, model="claude-haiku-5-5"),
        _with_suite(evidence_inputs, repeats=3),
        _with_suite(evidence_inputs, threshold=1.0),
        _with_suite(evidence_inputs, rubric=[c.model_copy(update={"deal_breaker": not c.deal_breaker}) for c in s.rubric]),
        _with_suite(evidence_inputs, rubric=s.rubric[:-1]),
    ]
    for change in changed:
        assert fingerprint(**{**evidence_inputs, **change}) != base, change.keys()


def test_fingerprint_ignores_example_order_names_labels_and_kinds(evidence_inputs):
    base = fingerprint(**evidence_inputs)
    cases = evidence_inputs["cases"]
    relabelled = [c.model_copy(update={"name": c.name + " (renamed)", "label": "Other Co", "kind": "messy"})
                  for c in cases]
    assert fingerprint(**{**evidence_inputs, "cases": list(reversed(cases))}) == base
    assert fingerprint(**{**evidence_inputs, "cases": relabelled}) == base


# record shapes ---------------------------------------------------------------------
def _record(**changes):
    v = VersionSnapshot(id="v2", number=2, text="Old")
    data = dict(
        id="20261105-aaaa", suite_id="followup-email", source="live",
        candidate=VersionSnapshot(id="v3", number=3, text="New"), baseline=v,
        examples=[ExampleSnapshot(id="a", name="A", input="x")], qualities=_rubric(),
        model="claude-sonnet-5-5", repetitions=1, threshold=0.5, reviewer_model="claude-sonnet-5-5",
        items=[ItemResult(version_id="v3", case_id="a", repetition=1, status="complete",
                          answer_key="k1", review_key="r1")],
        classifications={"a": "better"}, status="complete", verdict="Ready to roll out",
        started="2026-11-05T10:00:00Z", finished="2026-11-05T10:01:00Z", fingerprint="f" * 64,
    )
    data.update(changes)
    return EvaluationRecord(**data)


def test_records_round_trip_through_json():
    rec = _record()
    assert EvaluationRecord.model_validate_json(rec.model_dump_json()) == rec
    blob = AnswerBlob(key="k1", text="Hello", model="m")
    assert AnswerBlob.model_validate_json(blob.model_dump_json()) == blob
    assert blob.text_hash == text_hash("Hello")
    review = ReviewBlob(key="r1", answer_hash=blob.text_hash, scores={"facts": Score(score=4, reason="Fine.")},
                        reviewer_model="m")
    assert ReviewBlob.model_validate_json(review.model_dump_json()) == review


def test_records_and_blobs_are_frozen():
    rec = _record()
    with pytest.raises(ValidationError):
        rec.status = "incomplete"
    with pytest.raises(ValidationError):
        AnswerBlob(key="k", text="t", model="m").text = "changed"


def test_only_a_complete_record_gets_a_verdict():
    with pytest.raises(ValidationError, match="only a complete test gets a verdict"):
        _record(status="incomplete")
    assert _record(status="incomplete", verdict=None).verdict is None


def test_a_complete_record_has_every_item_and_classification():
    missing = ItemResult(version_id="v3", case_id="a", repetition=1, status="cut_off",
                         message="This answer was cut off at the length limit.")
    with pytest.raises(ValidationError, match="every answer and review"):
        _record(items=[missing])
    with pytest.raises(ValidationError, match="classifies every example"):
        _record(classifications={})


def test_a_running_record_has_no_finish_or_verdict():
    rec = _record(status="running", verdict=None, finished="")
    assert not rec.final
    with pytest.raises(ValidationError):
        _record(status="running", verdict=None)
    assert all(_record(status=s, verdict=None).final for s in ("incomplete", "interrupted"))


def test_a_complete_item_references_its_answer_and_review():
    with pytest.raises(ValidationError):
        ItemResult(version_id="v3", case_id="a", repetition=1, status="complete", answer_key="k1")


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        AnswerBlob(key="k", text="t", model="m", surprise=True)


def test_override_set_needs_a_reason_but_remove_does_not():
    with pytest.raises(ValidationError):
        OverrideEvent(id="o1", fingerprint="f", case_id="a", call="not_worse", result="same", reason="  ")
    ok = OverrideEvent(id="o1", fingerprint="f", case_id="a", call="not_worse", result="same",
                       reason="The answer is in the PS.")
    assert ok.action == "set"
    removed = OverrideEvent(id="o2", fingerprint="f", case_id="a", action="remove")
    assert removed.reason == ""
    with pytest.raises(ValidationError):
        OverrideEvent(id="o3", fingerprint="f", case_id="a", call="not_worse", result="same", reason="x" * 201)


def test_snapshot_models_match_the_app_models():
    """Examples and versions snapshot every field a person can see, so an old record shows its own evidence."""
    case_fields = set(Case.model_fields)
    assert case_fields <= set(ExampleSnapshot.model_fields)
    assert {"id", "number", "title", "note", "created", "text"} <= set(VersionSnapshot.model_fields)
