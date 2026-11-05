"""Evidence: the keys that decide when a saved result may be reused, and the records a test writes.

A saved result may be reused only when every input that produced it is identical
(spec section 5). There are two keys:

- Answer key: version text, example input, generation model, generation settings,
  repetition number and the generation wrapper revision. Any change means a new
  answer, and so a new review.
- Review key: the answer's content hash, the qualities (names, 5 and 1
  descriptions, deal-breaker flags), the reviewer model and the reviewer prompt
  revision. A change here reuses the answer and reruns only the review.

Quick checks are code, not model calls, so they are in neither key. They are in the
evidence fingerprint, which covers everything that affects generation, scoring or
comparison. A record whose fingerprint no longer matches the current inputs is stale
for rollout.

Bump GENERATION_WRAPPER_REVISION when the way a version and an example are sent to
Claude changes, and REVIEWER_PROMPT_REVISION when the AI reviewer's prompt changes.
tests/test_evidence.py fails until you do.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Case, Check, Criterion, Score, Suite
from .provider import MAX_OUTPUT_TOKENS
from .scoring import DEAL_BREAKER_CUTOFF

GENERATION_WRAPPER_REVISION = "1"  # version text as the system prompt, example as the user message
REVIEWER_PROMPT_REVISION = "1"  # judge.JUDGE_SYSTEM and judge.judge_prompt
GENERATION_SETTINGS: dict[str, Any] = {"max_tokens": MAX_OUTPUT_TOKENS}


# hashing -------------------------------------------------------------------------
def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("ascii")).hexdigest()


def text_hash(text: str) -> str:
    """The content hash of an answer, used in the review key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def answer_key(*, version_text: str, example_input: str, model: str, repetition: int,
               settings: dict[str, Any] | None = None,
               wrapper_revision: str = GENERATION_WRAPPER_REVISION) -> str:
    """Same key means the same request to Claude, so the saved answer can be reused."""
    if repetition < 1:
        raise ValueError("repetitions are numbered from 1")
    return _hash({
        "kind": "answer",
        "version_text": version_text,
        "example_input": example_input,
        "model": model,
        "settings": GENERATION_SETTINGS if settings is None else settings,
        "repetition": repetition,
        "wrapper_revision": wrapper_revision,
    })


def qualities_payload(rubric: list[Criterion]) -> list[dict[str, Any]]:
    """Everything about the qualities the AI reviewer sees or the verdict uses, deal-breaker flags included."""
    return [{"id": c.id, "name": c.name, "five_looks_like": c.five_looks_like,
             "one_looks_like": c.one_looks_like, "deal_breaker": c.deal_breaker} for c in rubric]


def review_key(*, answer_hash: str, rubric: list[Criterion], reviewer_model: str,
               reviewer_revision: str = REVIEWER_PROMPT_REVISION) -> str:
    """Same key means the same answer reviewed the same way, so the saved review can be reused."""
    return _hash({
        "kind": "review",
        "answer_hash": answer_hash,
        "qualities": qualities_payload(rubric),
        "reviewer_model": reviewer_model,
        "reviewer_revision": reviewer_revision,
    })


def fingerprint(*, source: str, suite: Suite, cases: list[Case], candidate_id: str, candidate_text: str,
                baseline_id: str, baseline_text: str, reviewer_model: str,
                settings: dict[str, Any] | None = None,
                wrapper_revision: str = GENERATION_WRAPPER_REVISION,
                reviewer_revision: str = REVIEWER_PROMPT_REVISION) -> str:
    """A hash over everything that affects generation, scoring or comparison.

    Example names, labels and kinds are left out (they change no answer, score or
    verdict), and examples are sorted by id, so reordering them is not a change.
    """
    return _hash({
        "kind": "evidence",
        "source": source,
        "candidate": {"id": candidate_id, "text": candidate_text},
        "baseline": {"id": baseline_id, "text": baseline_text},
        "examples": sorted([c.id, c.input] for c in cases),
        "qualities": qualities_payload(suite.rubric),
        "checks": [c.model_dump() for c in suite.checks],
        "model": suite.model,
        "settings": GENERATION_SETTINGS if settings is None else settings,
        "repetitions": suite.repeats,
        "threshold": suite.threshold,
        "deal_breaker_cutoff": DEAL_BREAKER_CUTOFF,
        "reviewer_model": reviewer_model,
        "wrapper_revision": wrapper_revision,
        "reviewer_revision": reviewer_revision,
    })


# records ---------------------------------------------------------------------------
class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswerBlob(_Frozen):
    """One answer, stored once under its answer key. Never changed after it is written."""

    key: str
    text: str
    stop_reason: str = "end_turn"
    model: str
    settings: dict[str, Any] = Field(default_factory=lambda: dict(GENERATION_SETTINGS))
    input_tokens: int = 0
    output_tokens: int = 0
    failed_input_tokens: int = 0
    failed_output_tokens: int = 0
    attempts: int = 1
    created: str = ""

    @property
    def text_hash(self) -> str:
        return text_hash(self.text)


class ReviewBlob(_Frozen):
    """One AI review of one answer, stored once under its review key."""

    key: str
    answer_hash: str
    scores: dict[str, Score]
    reviewer_model: str
    reviewer_revision: str = REVIEWER_PROMPT_REVISION
    input_tokens: int = 0
    output_tokens: int = 0
    failed_input_tokens: int = 0
    failed_output_tokens: int = 0
    attempts: int = 1
    created: str = ""


class CheckOutcome(_Frozen):
    label: str
    passed: bool
    detail: str = ""


class MissDetail(_Frozen):
    """A deal-breaker miss: which quality, in which repetition, with the original score and reason."""

    quality_id: str
    quality_name: str
    repetition: int = Field(ge=1)
    score: int = Field(ge=1, le=5)
    reason: str = ""


ItemStatus = Literal["complete", "missing", "cut_off", "failed"]


class ItemResult(_Frozen):
    """One version on one example in one repetition: references to its answer and review, plus what code found."""

    version_id: str
    case_id: str
    repetition: int = Field(ge=1)
    status: ItemStatus
    message: str = ""
    answer_key: str = ""
    review_key: str = ""
    stop_reason: str = ""
    checks: list[CheckOutcome] = []
    deal_breaker_misses: list[MissDetail] = []
    input_tokens: int = 0  # every call for this item, failed attempts included
    output_tokens: int = 0

    @model_validator(mode="after")
    def _complete_has_both(self) -> "ItemResult":
        if self.status == "complete" and not (self.answer_key and self.review_key):
            raise ValueError("a complete item needs both its answer key and its review key")
        return self


class ExampleSnapshot(_Frozen):
    id: str
    name: str
    label: str = ""
    kind: str = "everyday"
    input: str


class VersionSnapshot(_Frozen):
    id: str
    number: int
    title: str = ""
    note: str = ""
    created: str = ""
    text: str


Classification = Literal["worse", "better", "same"]


class OverrideEvent(_Frozen):
    """A person's call on one example for one evaluation's evidence, or its removal.

    Overrides are never deleted: removing one appends an event with action "remove".
    They apply only while the evidence fingerprint matches, and never hide a missed
    deal-breaker.
    """

    id: str
    fingerprint: str
    evaluation_id: str = ""
    case_id: str
    action: Literal["set", "remove"] = "set"
    call: Literal["not_worse", "not_better", "other"] | None = None
    result: Classification | None = None
    reason: str = Field(default="", max_length=200)
    created: str = ""

    @model_validator(mode="after")
    def _set_needs_a_call_and_reason(self) -> "OverrideEvent":
        if self.action == "set" and not (self.call and self.result and self.reason.strip()):
            raise ValueError("an override needs a call, a result and a one-line reason")
        return self


EvaluationStatus = Literal["running", "complete", "incomplete", "interrupted"]
FINAL_STATUSES = ("complete", "incomplete", "interrupted")


class EvaluationRecord(_Frozen):
    """Everything one test run used and found. Written once; final records never change."""

    id: str
    suite_id: str
    kind: Literal["comparison", "baseline"] = "comparison"  # baseline: one version scored on its own
    source: Literal["recorded", "live"]
    legacy: bool = False  # saved before V3: some details weren't recorded
    # instruction context
    candidate: VersionSnapshot
    baseline: VersionSnapshot
    baseline_in_use: bool = False
    instruction_type: str = "prompt"
    # configuration
    examples: list[ExampleSnapshot]
    qualities: list[Criterion]
    checks: list[Check] = []
    model: str
    generation_settings: dict[str, Any] = Field(default_factory=lambda: dict(GENERATION_SETTINGS))
    repetitions: int = Field(ge=1)
    threshold: float
    reviewer_model: str
    reviewer_revision: str = REVIEWER_PROMPT_REVISION
    wrapper_revision: str = GENERATION_WRAPPER_REVISION
    # results
    items: list[ItemResult] = []
    classifications: dict[str, Classification] = {}  # per example, before any override
    # outcome
    overrides_applied: list[OverrideEvent] = []
    counts: dict[str, int] = {}
    avg_baseline: float | None = None
    avg_candidate: float | None = None
    verdict: str | None = None
    status: EvaluationStatus = "running"
    started: str
    finished: str = ""
    fingerprint: str

    @model_validator(mode="after")
    def _consistent(self) -> "EvaluationRecord":
        if self.status == "running" and (self.finished or self.verdict):
            raise ValueError("a running test has no finish time and no verdict yet")
        if self.status != "complete" and self.verdict is not None:
            raise ValueError("only a complete test gets a verdict")
        if self.status == "complete":
            if any(i.status != "complete" for i in self.items):
                raise ValueError("a complete test has every answer and review")
            if self.kind == "comparison" and set(self.classifications) != {e.id for e in self.examples}:
                raise ValueError("a complete comparison classifies every example")
        return self

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES
