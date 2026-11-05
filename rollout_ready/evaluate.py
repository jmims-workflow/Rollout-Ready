"""Evaluations: run only what is missing, and record what a test used and found.

A test compares a candidate version with a baseline version (or, for a new set,
scores one version on its own). For every example and repetition it needs an
answer and a review. Saved answers and reviews are reused only when their keys
match (evidence.py), so a test never pays twice for the same call.

Every live test writes one evaluation record: first as "running", then once more
as final ("complete", "incomplete" or "interrupted"). A final record never
changes. A record left "running" by a server restart is marked "interrupted" the
next time it is read, and finishing it runs only the missing items.

Demo mode never writes anything: its records are built the same way every time
from the recorded results that ship with the app (source "recorded").
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import scoring
from .checks import run_checks
from .config import DEFAULT_JUDGE_MODEL
from .evidence import (GENERATION_SETTINGS, GENERATION_WRAPPER_REVISION, REVIEWER_PROMPT_REVISION, AnswerBlob,
                       CheckOutcome, EvaluationRecord, ExampleSnapshot, ItemResult, MissDetail, OverrideEvent,
                       ReviewBlob, VersionSnapshot, answer_key, fingerprint, review_key, text_hash)
from .judge import judge_output
from .models import Case, Override, Run, Suite, Version, VersionResults
from .provider import CUT_OFF, CUT_OFF_MESSAGE, Provider, ProviderError, call_with_retries
from .storage import Store, SuiteError, input_hash, rubric_hash

Progress = Callable[[int, int, str], None]

# Records being written by a test in this process. A "running" record that isn't
# here was left behind by a restart, so it is interrupted.
_active: set[str] = set()
_active_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    """Sortable by start time, unique within the set."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]


# evidence: a record plus the answers and reviews it points to -----------------------
@dataclass
class Evidence:
    record: EvaluationRecord
    answers: dict[str, AnswerBlob] = field(default_factory=dict)
    reviews: dict[str, ReviewBlob] = field(default_factory=dict)
    saved: bool = True  # False for a record built on the fly (demo, or live results not yet tested together)

    def runs(self, version_id: str, case_id: str) -> list[Run] | None:
        """The runs for one version on one example, in repetition order, or None if any is missing."""
        items = sorted((i for i in self.record.items if i.version_id == version_id and i.case_id == case_id),
                       key=lambda i: i.repetition)
        if len(items) < self.record.repetitions or any(i.status != "complete" for i in items):
            return None
        out = []
        for i in items[: self.record.repetitions]:
            a, r = self.answers.get(i.answer_key), self.reviews.get(i.review_key)
            if a is None or r is None:
                return None
            out.append(_run(a, r))
        return out


def _run(a: AnswerBlob, r: ReviewBlob) -> Run:
    return Run(output=a.text, scores=r.scores,
               input_tokens=a.input_tokens + a.failed_input_tokens,
               output_tokens=a.output_tokens + a.failed_output_tokens,
               judge_input_tokens=r.input_tokens + r.failed_input_tokens,
               judge_output_tokens=r.output_tokens + r.failed_output_tokens)


def _version_snapshot(v: Version) -> VersionSnapshot:
    return VersionSnapshot(id=v.id, number=v.number, title=v.title, note=v.note, created=v.created, text=v.text)


def _example_snapshot(c: Case) -> ExampleSnapshot:
    return ExampleSnapshot(id=c.id, name=c.name, label=c.label, kind=c.kind, input=c.input)


def _item(version_id: str, case: Case, repetition: int, suite: Suite, answer: AnswerBlob | None,
          review: ReviewBlob | None, *, status: str | None = None, message: str = "", stop_reason: str = "",
          extra_in: int = 0, extra_out: int = 0) -> ItemResult:
    """One item. A complete one carries its quick-check results and any deal-breaker misses."""
    tokens_in = extra_in + (answer.input_tokens + answer.failed_input_tokens if answer else 0) \
        + (review.input_tokens + review.failed_input_tokens if review else 0)
    tokens_out = extra_out + (answer.output_tokens + answer.failed_output_tokens if answer else 0) \
        + (review.output_tokens + review.failed_output_tokens if review else 0)
    if status is None:
        status = "complete" if answer and review else "missing"
    checks, misses = [], []
    if status == "complete":
        checks = [CheckOutcome(label=c.label, passed=c.passed, detail=c.detail) for c in run_checks(suite.checks, answer.text)]
        misses = [MissDetail(quality_id=c.id, quality_name=c.name, repetition=repetition, score=score, reason=reason)
                  for c, _rep, score, reason in scoring.deal_breaker_misses([_run(answer, review)], suite.rubric)]
    return ItemResult(version_id=version_id, case_id=case.id, repetition=repetition, status=status, message=message,
                      answer_key=answer.key if answer else "", review_key=review.key if review else "",
                      stop_reason=stop_reason or (answer.stop_reason if answer else ""),
                      checks=checks, deal_breaker_misses=misses, input_tokens=tokens_in, output_tokens=tokens_out)


# planning ----------------------------------------------------------------------------
@dataclass
class Planned:
    version: Version
    case: Case
    repetition: int
    answer_key: str
    answer: AnswerBlob | None
    review: ReviewBlob | None

    @property
    def calls(self) -> int:
        """Calls still needed: an answer and its review, or just the review."""
        return 2 if self.answer is None else (1 if self.review is None else 0)


def _review_key(suite: Suite, answer: AnswerBlob, reviewer_model: str) -> str:
    return review_key(answer_hash=answer.text_hash, rubric=suite.rubric, reviewer_model=reviewer_model)


def plan(store: Store, suite: Suite, cases: list[Case], versions: list[Version], reviewer_model: str) -> list[Planned]:
    """Every item a test needs, with whatever saved answer and review can be reused."""
    out = []
    for v in versions:
        for case in cases:
            for rep in range(1, suite.repeats + 1):
                akey = answer_key(version_text=v.text, example_input=case.input, model=suite.model, repetition=rep)
                answer = store.load_answer(suite.id, akey)
                review = store.load_review(suite.id, _review_key(suite, answer, reviewer_model)) if answer else None
                out.append(Planned(v, case, rep, akey, answer, review))
    return out


def _load(store: Store, suite_id: str, baseline_id: str, candidate_id: str):
    suite = store.load_suite(suite_id)
    cases = store.load_cases(suite_id)
    baseline = store.load_version(suite_id, baseline_id)
    candidate = store.load_version(suite_id, candidate_id)
    versions = [baseline] if baseline_id == candidate_id else [baseline, candidate]
    return suite, cases, baseline, candidate, versions


def current_fingerprint(store: Store, suite_id: str, baseline_id: str, candidate_id: str,
                        reviewer_model: str = DEFAULT_JUDGE_MODEL, source: str = "live") -> str:
    suite, cases, baseline, candidate, _ = _load(store, suite_id, baseline_id, candidate_id)
    return _fingerprint(source, suite, cases, baseline, candidate, reviewer_model)


def _fingerprint(source, suite, cases, baseline, candidate, reviewer_model) -> str:
    return fingerprint(source=source, suite=suite, cases=cases, candidate_id=candidate.id, candidate_text=candidate.text,
                       baseline_id=baseline.id, baseline_text=baseline.text, reviewer_model=reviewer_model)


def calls_needed(store: Store, suite_id: str, version_ids: list[str], reviewer_model: str = DEFAULT_JUDGE_MODEL) -> int:
    suite, cases = store.load_suite(suite_id), store.load_cases(suite_id)
    versions = [store.load_version(suite_id, v) for v in dict.fromkeys(version_ids)]
    return sum(p.calls for p in plan(store, suite, cases, versions, reviewer_model))


def cases_needing_calls(store: Store, suite: Suite, cases: list[Case], version_id: str,
                        reviewer_model: str = DEFAULT_JUDGE_MODEL) -> list[Case]:
    """Examples where this version still needs an answer or a review, in any repetition."""
    version = store.load_version(suite.id, version_id)
    needed = {p.case.id for p in plan(store, suite, cases, [version], reviewer_model) if p.calls}
    return [c for c in cases if c.id in needed]


# building a record --------------------------------------------------------------------
def _in_use(store: Store, suite_id: str) -> str:
    from .releases import in_use_for  # releases builds on this module
    try:
        return in_use_for(store, suite_id)
    except SuiteError:
        return ""


def _new_record(suite: Suite, cases: list[Case], baseline: Version, candidate: Version, reviewer_model: str,
                source: str, fp: str, record_id: str, started: str, items=(), status="running",
                in_use_id: str = "") -> EvaluationRecord:
    return EvaluationRecord(
        id=record_id, suite_id=suite.id, kind="baseline" if baseline.id == candidate.id else "comparison",
        source=source, candidate=_version_snapshot(candidate), baseline=_version_snapshot(baseline),
        baseline_in_use=baseline.id == in_use_id, instruction_type=suite.type,
        examples=[_example_snapshot(c) for c in cases], qualities=list(suite.rubric), checks=list(suite.checks),
        model=suite.model, generation_settings=dict(GENERATION_SETTINGS), repetitions=suite.repeats,
        threshold=suite.threshold, reviewer_model=reviewer_model, reviewer_revision=REVIEWER_PROMPT_REVISION,
        wrapper_revision=GENERATION_WRAPPER_REVISION, items=list(items), status=status, started=started,
        fingerprint=fp,
    )


# overrides ------------------------------------------------------------------------------
def active_override_events(events: list[OverrideEvent], fp: str) -> dict[str, OverrideEvent]:
    """The overrides in force for this evidence, by example: the latest "set" not followed by a "remove".

    Events for any other fingerprint are ignored, so an override never carries over to changed evidence.
    """
    active: dict[str, OverrideEvent] = {}
    for e in events:
        # Demo mode keeps a 16-character fingerprint in the cookie (demo_session.py).
        if e.fingerprint != fp and not (len(e.fingerprint) == 16 and fp.startswith(e.fingerprint)):
            continue
        if e.action == "remove":
            active.pop(e.case_id, None)
        else:
            active[e.case_id] = e
    return active


def as_overrides(active: dict[str, OverrideEvent]) -> dict[str, Override]:
    return {cid: Override(call=e.call, result=e.result, reason=e.reason, created=e.created) for cid, e in active.items()}


def override_event(fp: str, evaluation_id: str, case_id: str, *, call=None, result=None, reason: str = "",
                   remove: bool = False) -> OverrideEvent:
    return OverrideEvent(id=uuid.uuid4().hex, fingerprint=fp, evaluation_id=evaluation_id, case_id=case_id,
                         action="remove" if remove else "set", call=None if remove else call,
                         result=None if remove else result, reason="" if remove else reason, created=now())


def finalize(evidence: Evidence, status: str | None = None, finished: str | None = None,
             overrides: dict[str, OverrideEvent] | None = None) -> EvaluationRecord:
    """The final record: status, the AI reviewer's classifications, and the outcome after any overrides in force.

    Classifications are always the AI reviewer's. Counts and verdict count each overridden
    example the way the person said, and a missed deal-breaker always blocks.
    """
    from .compare import comparison_from_evidence  # compare builds on this module

    rec = evidence.record
    all_done = bool(rec.items) and all(i.status == "complete" for i in rec.items)
    status = status or ("complete" if all_done else "incomplete")
    draft = Evidence(rec.model_copy(update={"status": "incomplete", "verdict": None}), evidence.answers,
                     evidence.reviews, evidence.saved)
    overrides = overrides or {}
    comp = comparison_from_evidence(draft, overrides=as_overrides(overrides))
    update = {"status": status, "finished": finished or now(), "verdict": None,
              "overrides_applied": [overrides[cid] for cid in sorted(overrides) if comp.row(cid)],
              "classifications": {r.case.id: r.ai_result for r in comp.rows} if rec.kind == "comparison" else {},
              "counts": {"worse": comp.count(scoring.WORSE), "better": comp.count(scoring.BETTER),
                         "same": comp.count(scoring.SAME)} if comp.rows else {},
              "avg_baseline": comp.avg_current if comp.rows else None,
              "avg_candidate": comp.avg_new if comp.rows else None}
    if status == "complete" and rec.kind == "comparison":
        update["verdict"] = scoring.verdict(comp.worse, comp.better,
                                            any(r.new_missed for r in comp.rows), complete=True)
    return EvaluationRecord.model_validate({**rec.model_dump(), **update})


# running a test ---------------------------------------------------------------------
@dataclass
class Outcome:
    record: EvaluationRecord
    calls: int  # calls that produced an answer or review (failed attempts not counted)


def _matching_final(store: Store, suite_id: str, baseline_id: str, candidate_id: str, fp: str) -> EvaluationRecord | None:
    for rid in reversed(store.evaluation_ids(suite_id)):
        rec = store.load_evaluation(suite_id, rid)
        if rec and rec.baseline.id == baseline_id and rec.candidate.id == candidate_id \
                and rec.fingerprint == fp and rec.status == "complete":
            return rec
    return None


def run_evaluation(store: Store, suite_id: str, baseline_id: str, candidate_id: str, provider: Provider,
                   reviewer_model: str, progress: Progress | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """Run only what is missing and write the evaluation record.

    An answer or review cut off at the length limit makes that item incomplete and the
    test carries on. Any other failure that retries can't fix stops the calls (the rest
    are left missing), finishes the record as incomplete, and is raised so the person
    sees why. Saved answers and reviews are always kept.
    """
    suite, cases, baseline, candidate, versions = _load(store, suite_id, baseline_id, candidate_id)
    fp = _fingerprint("live", suite, cases, baseline, candidate, reviewer_model)
    same = _matching_final(store, suite_id, baseline_id, candidate_id, fp)
    if same is not None:
        if progress:
            progress(0, 0, "Done")
        return Outcome(same, 0)

    planned = plan(store, suite, cases, versions, reviewer_model)
    total = sum(p.calls for p in planned)
    record = _new_record(suite, cases, baseline, candidate, reviewer_model, "live", fp, new_id(), now(),
                         in_use_id=_in_use(store, suite_id))
    store.save_evaluation(record)
    with _active_lock:
        _active.add(record.id)
    ev = Evidence(record)
    items: list[ItemResult] = []
    done = calls = 0
    stopped: ProviderError | None = None
    try:
        for p in planned:
            if stopped is not None:
                items.append(_item(p.version.id, p.case, p.repetition, suite, None, None, status="missing"))
                continue
            answer, review = p.answer, p.review
            try:
                if answer is None:
                    if progress:
                        progress(done, total, f"Writing an answer for “{p.case.name}”")
                    made = call_with_retries(lambda p=p: provider.generate(
                        model=suite.model, prompt=p.version.text, user_input=p.case.input), sleep=sleep)
                    gen = made.value
                    done += 1
                    if gen.cut_off:  # never saved or scored, never retried automatically
                        done += 1  # its review is skipped
                        items.append(_item(p.version.id, p.case, p.repetition, suite, None, None, status="cut_off",
                                           message=CUT_OFF_MESSAGE, stop_reason=gen.stop_reason,
                                           extra_in=gen.input_tokens + made.failed_input_tokens,
                                           extra_out=gen.output_tokens + made.failed_output_tokens))
                        continue
                    answer = AnswerBlob(key=p.answer_key, text=gen.text, stop_reason=gen.stop_reason, model=suite.model,
                                        input_tokens=gen.input_tokens, output_tokens=gen.output_tokens,
                                        failed_input_tokens=made.failed_input_tokens,
                                        failed_output_tokens=made.failed_output_tokens,
                                        attempts=made.attempts, created=now())
                    store.save_answer(suite_id, answer, version_id=p.version.id, case=p.case)
                    calls += 1
                    review = store.load_review(suite_id, _review_key(suite, answer, reviewer_model))
                if review is None:
                    if progress:
                        progress(done, total, f"Scoring the answer for “{p.case.name}”")
                    try:
                        scores, parsed = judge_output(provider, reviewer_model, suite, p.case, answer.text, sleep=sleep)
                    except ProviderError as exc:
                        if exc.kind != "cut_off":
                            raise
                        done += 1
                        items.append(_item(p.version.id, p.case, p.repetition, suite, answer, None, status="cut_off",
                                           message=str(exc), stop_reason=CUT_OFF,
                                           extra_in=exc.input_tokens, extra_out=exc.output_tokens))
                        continue
                    done += 1
                    review = ReviewBlob(key=_review_key(suite, answer, reviewer_model), answer_hash=answer.text_hash,
                                        scores=scores, reviewer_model=reviewer_model,
                                        input_tokens=parsed.input_tokens - parsed.failed_input_tokens,
                                        output_tokens=parsed.output_tokens - parsed.failed_output_tokens,
                                        failed_input_tokens=parsed.failed_input_tokens,
                                        failed_output_tokens=parsed.failed_output_tokens,
                                        attempts=parsed.attempts, created=now())
                    store.save_review(suite_id, review, version_id=p.version.id, case=p.case)
                    calls += 1
            except ProviderError as exc:
                stopped = exc
                items.append(_item(p.version.id, p.case, p.repetition, suite, answer, None, status="failed",
                                   message=str(exc), extra_in=exc.input_tokens, extra_out=exc.output_tokens))
                continue
            ev.answers[answer.key] = answer
            ev.reviews[review.key] = review
            items.append(_item(p.version.id, p.case, p.repetition, suite, answer, review))
        ev.record = record.model_copy(update={"items": items})
        final = finalize(ev, overrides=active_override_events(store.override_events(suite_id), fp))
        store.finish_evaluation(final)
    finally:
        with _active_lock:
            _active.discard(record.id)
    if progress:
        progress(total, total, "Done")
    if stopped is not None:
        raise stopped
    return Outcome(final, calls)


# reading records ----------------------------------------------------------------------
def hydrate(store: Store, record: EvaluationRecord) -> Evidence:
    """A saved record with the answers and reviews it points to."""
    ev = Evidence(record)
    for i in record.items:
        if i.answer_key and i.answer_key not in ev.answers:
            a = store.load_answer(record.suite_id, i.answer_key)
            if a:
                ev.answers[a.key] = a
        if i.review_key and i.review_key not in ev.reviews:
            r = store.load_review(record.suite_id, i.review_key)
            if r:
                ev.reviews[r.key] = r
    return ev


def mark_interrupted(store: Store, record: EvaluationRecord) -> EvaluationRecord:
    """A running record with no test behind it (the server restarted) becomes interrupted, once."""
    if record.status != "running":
        return record
    with _active_lock:
        if record.id in _active:
            return record
    final = finalize(hydrate(store, record), status="interrupted")
    try:
        store.finish_evaluation(final)
    except Exception:  # someone else finished it first: read what they wrote
        return store.load_evaluation(record.suite_id, record.id) or record
    return final


def load(store: Store, suite_id: str, evaluation_id: str) -> Evidence | None:
    rec = store.load_evaluation(suite_id, evaluation_id)
    return hydrate(store, mark_interrupted(store, rec)) if rec else None


def records_for_pair(store: Store, suite_id: str, baseline_id: str, candidate_id: str) -> list[EvaluationRecord]:
    """Saved records for this pair, newest first."""
    out = []
    for rid in reversed(store.evaluation_ids(suite_id)):
        rec = store.load_evaluation(suite_id, rid)
        if rec and rec.baseline.id == baseline_id and rec.candidate.id == candidate_id:
            out.append(mark_interrupted(store, rec))
    return out


def latest_record(store: Store, suite_id: str, baseline_id: str, candidate_id: str) -> EvaluationRecord | None:
    found = records_for_pair(store, suite_id, baseline_id, candidate_id)
    return found[0] if found else None


def versions_with_results(store: Store, suite_id: str) -> set[str]:
    """Versions with at least one complete answer and review in a saved record."""
    out: set[str] = set()
    for rid in store.evaluation_ids(suite_id):
        rec = store.load_evaluation(suite_id, rid)
        if rec:
            out |= {i.version_id for i in rec.items if i.status == "complete"}
    return out


def is_stale(store: Store, record: EvaluationRecord) -> bool:
    """True when the inputs changed since this test, so it can't support a rollout."""
    try:
        fp = current_fingerprint(store, record.suite_id, record.baseline.id, record.candidate.id,
                                 record.reviewer_model, record.source)
    except Exception:
        return True
    return fp != record.fingerprint


def live_evidence(store: Store, suite_id: str, baseline_id: str, candidate_id: str,
                  reviewer_model: str = DEFAULT_JUDGE_MODEL) -> Evidence:
    """What live mode shows for a pair.

    The newest saved record made with the current inputs, if there is one. Otherwise a
    record built on the fly from saved answers and reviews that match the current
    inputs (not saved, so it can't support a rollout until the pair is tested together).
    """
    suite, cases, baseline, candidate, versions = _load(store, suite_id, baseline_id, candidate_id)
    fp = _fingerprint("live", suite, cases, baseline, candidate, reviewer_model)
    for rec in records_for_pair(store, suite_id, baseline_id, candidate_id):
        if rec.fingerprint == fp and rec.final:
            return hydrate(store, rec)
    ev = Evidence(_new_record(suite, cases, baseline, candidate, reviewer_model, "live", fp, "", now(),
                              in_use_id=_in_use(store, suite_id)), saved=False)
    items = []
    for p in plan(store, suite, cases, versions, reviewer_model):
        if p.answer and p.review:
            ev.answers[p.answer.key] = p.answer
            ev.reviews[p.review.key] = p.review
        items.append(_item(p.version.id, p.case, p.repetition, suite, p.answer, p.review))
    ev.record = ev.record.model_copy(update={"items": items})
    ev.record = finalize(ev, finished=ev.record.started)
    return ev


# demo: records built from the recorded results that ship with the app -----------------
def _entry_matches(entry, suite: Suite, case: Case) -> bool:
    if entry.model and entry.model != suite.model:
        return False
    if entry.rubric_hash and entry.rubric_hash != rubric_hash(suite):
        return False
    if entry.input_hash and entry.input_hash != input_hash(case):
        return False
    return len(entry.runs) >= suite.repeats


def recorded_id(baseline_id: str, candidate_id: str) -> str:
    return f"recorded-{baseline_id}-{candidate_id}"


def parse_recorded_id(evaluation_id: str) -> tuple[str, str] | None:
    """(baseline, candidate) for a recorded demo record's id, else None."""
    import re
    m = re.fullmatch(r"recorded-(v\d+)-(v\d+)", evaluation_id or "")
    return (m.group(1), m.group(2)) if m else None


def recorded_evidence(store: Store, suite_id: str, baseline_id: str, candidate_id: str) -> Evidence:
    """The same record every time, from recorded results. Never written anywhere."""
    suite, cases, baseline, candidate, versions = _load(store, suite_id, baseline_id, candidate_id)
    recorded = {v.id: store.load_recorded(suite_id, v.id) or VersionResults(version=v.id, source="recorded")
                for v in versions}
    reviewer = next((r.judge_model for r in recorded.values() if r.judge_model), DEFAULT_JUDGE_MODEL)
    fp = _fingerprint("recorded", suite, cases, baseline, candidate, reviewer)
    ran = max((r.ran_at for r in recorded.values()), default="") or now()
    # A stable id, so the history that ships with the app can point at it.
    ev = Evidence(_new_record(suite, cases, baseline, candidate, reviewer, "recorded", fp,
                              recorded_id(baseline.id, candidate.id), ran,
                              in_use_id=_in_use(store, suite_id)), saved=False)
    items = []
    for v in versions:
        res = recorded[v.id]
        for case in cases:
            entry = res.cases.get(case.id)
            usable = entry is not None and _entry_matches(entry, suite, case)
            for rep in range(1, suite.repeats + 1):
                if not usable:
                    items.append(ItemResult(version_id=v.id, case_id=case.id, repetition=rep, status="missing"))
                    continue
                run = entry.runs[rep - 1]
                akey = "recorded-" + answer_key(version_text=v.text, example_input=case.input, model=suite.model,
                                                repetition=rep)
                a = AnswerBlob(key=akey, text=run.output, model=entry.model or suite.model,
                               input_tokens=run.input_tokens, output_tokens=run.output_tokens, created=res.ran_at)
                r = ReviewBlob(key="recorded-" + review_key(answer_hash=text_hash(run.output), rubric=suite.rubric,
                                                            reviewer_model=reviewer),
                               answer_hash=a.text_hash, scores=run.scores, reviewer_model=reviewer,
                               input_tokens=run.judge_input_tokens, output_tokens=run.judge_output_tokens,
                               created=res.ran_at)
                ev.answers[a.key], ev.reviews[r.key] = a, r
                items.append(_item(v.id, case, rep, suite, a, r))
    ev.record = ev.record.model_copy(update={"items": items})
    ev.record = finalize(ev, finished=ran)
    return ev


# results saved before V3 -------------------------------------------------------------
def has_legacy_results(store: Store, suite_id: str, version_ids: list[str]) -> bool:
    """True when older live results (runs/<version>.json) exist. They are shown as incomplete and never reused."""
    for v in dict.fromkeys(version_ids):
        try:
            if store.load_runs(suite_id, v):
                return True
        except Exception:
            return True
    return False
