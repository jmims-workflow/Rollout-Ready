"""Release history: what the owner confirmed is in use, and why. Rolling out is recorded, not performed.

History is an append-only list of events, never edited or deleted:

- baseline   the version in use when a set was added ("Recorded as in use by the owner")
- rollout    a version put into use on a complete, current Ready to roll out result
- exception  "Roll out anyway": put into use on any other complete result, with a reason;
             it keeps the evaluation's own verdict and is never described as passing
- return     "Put back into use": an earlier version in use again, with a reason

A set's history is the events that ship with the app (suites/<id>/history.json) followed
by the ones added since (runs/releases.json in live mode, the visitor's own folder in public
mode, the signed session cookie in demo mode).

Version status is derived from history, never stored: In use (the latest event's version),
Retired (was in use, then superseded) or Draft (never confirmed in use).

Every check runs again when the confirm button is submitted, not only when the page loads.
A confirmation carries a one-time token: submitting it twice shows the release it already
made instead of making a second one.
"""

from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import scoring
from .storage import Store, SuiteError

IN_USE_HELP = "Last confirmed in use by the owner. Rollout Ready does not monitor changes in the external tool."
NAMES_HELP = "Names are entered by the owner, not verified"
RECORDED_NOTE = "Rollout Ready records your change. It doesn't update your AI tool for you."
STALE = ("Your examples or what good looks like changed since this version was tested. "
         "Test it again before rolling out.")
MAX_REASON = 200
MAX_NAME = 60

ReleaseType = Literal["baseline", "rollout", "exception", "return"]
EXCEPTION_VERDICTS = (scoring.NOT_READY, scoring.NEEDS_REVIEW, scoring.NO_DIFFERENCE, scoring.READY)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReleaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    type: ReleaseType
    from_version: str = ""
    to_version: str
    timestamp: str
    confirmed_by: str = Field(default="", max_length=MAX_NAME)
    evaluation_id: str = ""
    evaluation_source: Literal["", "recorded", "live"] = ""
    verdict: str | None = None  # the evaluation's verdict at confirmation, after overrides
    override_ids: list[str] = []  # the overrides in force at confirmation
    reason: str = Field(default="", max_length=MAX_REASON)
    baseline_ack: bool = False  # "I understand this test didn't compare against the version in use."
    token: str = ""  # one-time token from the form

    @model_validator(mode="after")
    def _consistent(self) -> "ReleaseEvent":
        if self.type == "baseline":
            if self.from_version:
                raise ValueError("a baseline event has no earlier version")
        elif not self.from_version or self.from_version == self.to_version:
            raise ValueError("a release moves from one version to a different one")
        if self.type == "rollout" and not (self.evaluation_id and self.verdict == scoring.READY):
            raise ValueError("a rollout relies on a Ready to roll out evaluation")
        if self.type == "exception":
            if not (self.evaluation_id and self.verdict in EXCEPTION_VERDICTS):
                raise ValueError("roll out anyway relies on a complete evaluation and keeps its verdict")
        if self.type in ("exception", "return") and not self.reason.strip():
            raise ValueError("this release needs a one-line reason")
        return self

    @property
    def summary(self) -> str:
        """One plain line for history lists and the release record."""
        if self.type == "baseline":
            return "Recorded as in use by the owner."
        if self.type == "rollout":
            return "Put into use. Evaluation: Ready to roll out."
        if self.type == "exception":
            return f"Put into use after review. Evaluation: {self.verdict}. Reason: {self.reason}"
        return f"Returned to an earlier version. Reason: {self.reason}"

    @property
    def relied_on_sample_results(self) -> bool:
        return self.evaluation_source == "recorded"


# where events live -----------------------------------------------------------------------
class ReleaseLog(Protocol):
    """Appended events for one set. Shipped history is read separately."""

    def events(self) -> list[ReleaseEvent]: ...

    def append(self, event: ReleaseEvent) -> None: ...

    def lock(self): ...


class StoreLog:
    """Live and public mode: runs/releases.json through the storage interface."""

    def __init__(self, store: Store, suite_id: str):
        self.store, self.suite_id = store, suite_id

    def events(self) -> list[ReleaseEvent]:
        return [ReleaseEvent.model_validate(e) for e in self.store.release_events(self.suite_id)]

    def append(self, event: ReleaseEvent) -> None:
        self.store.append_release_event(self.suite_id, event.model_dump(mode="json"))

    def lock(self):
        return self.store.lock(self.suite_id)


class ListLog:
    """Demo mode: events kept by the caller (the signed session cookie). One request at a time."""

    def __init__(self, events: list[ReleaseEvent] | None = None):
        self._events = list(events or [])
        self.added: list[ReleaseEvent] = []

    def events(self) -> list[ReleaseEvent]:
        return list(self._events)

    def append(self, event: ReleaseEvent) -> None:
        self._events.append(event)
        self.added.append(event)

    def lock(self):
        return nullcontext()


def shipped(store: Store, suite_id: str) -> list[ReleaseEvent]:
    """The history that ships with the app (suites/<id>/history.json). Empty for other sets."""
    path = store.suite_dir(suite_id) / "history.json"
    if not path.exists():
        return []
    try:
        return [ReleaseEvent.model_validate(e) for e in json.loads(path.read_text(encoding="utf-8"))]
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SuiteError(f"{path} could not be read: {exc}") from exc


LEGACY_BASELINE_ID = "legacy-baseline"


def history(store: Store, suite_id: str, log: ReleaseLog) -> list[ReleaseEvent]:
    """Shipped events, then the ones added since.

    A set saved before V3 has neither. It gets one baseline event, built here and never
    written: its old in_use setting, or else its newest version.
    """
    events = [*shipped(store, suite_id), *log.events()]
    if events:
        return events
    ids = [v.id for v in store.versions(suite_id)]
    if not ids:
        return []
    legacy = store.load_suite(suite_id).in_use
    version = store.load_version(suite_id, legacy if legacy in ids else ids[-1])
    return [ReleaseEvent(id=LEGACY_BASELINE_ID, type="baseline", to_version=version.id,
                         timestamp=f"{version.created}T00:00:00Z" if version.created else "")]


# derived status ---------------------------------------------------------------------------
def in_use(events: list[ReleaseEvent], version_ids: list[str], legacy_in_use: str = "") -> str:
    """The version in the latest release event. A set with no history (saved before V3) uses its old setting."""
    for e in reversed(events):
        if e.to_version in version_ids:
            return e.to_version
    return legacy_in_use if legacy_in_use in version_ids else (version_ids[-1] if version_ids else "")


def in_use_for(store: Store, suite_id: str, log: ReleaseLog | None = None) -> str:
    suite = store.load_suite(suite_id)
    ids = [v.id for v in store.versions(suite_id)]
    return in_use(history(store, suite_id, log or StoreLog(store, suite_id)), ids, suite.in_use)


def status(version_id: str, events: list[ReleaseEvent], in_use_id: str) -> str:
    """In use, Retired (was in use and was superseded) or Draft (never confirmed in use)."""
    if version_id == in_use_id:
        return "In use"
    if any(e.to_version == version_id for e in events):
        return "Retired"
    return "Draft"


def since(events: list[ReleaseEvent], in_use_id: str) -> str:
    """When the version in use was last confirmed in use."""
    return next((e.timestamp for e in reversed(events) if e.to_version == in_use_id), "")


def changed_baseline_text(tested: int, against: int, now_in_use: int) -> str:
    return (f"This test compared version {tested} with version {against}. Version {now_in_use} is in use now, "
            f"so it doesn't show how version {tested} compares with what your team uses.")


# deciding --------------------------------------------------------------------------------
@dataclass
class Decision:
    """What the person submitted. Every field is checked again at submission."""
    kind: Literal["rollout", "exception", "return"]
    version_id: str
    token: str
    confirmed_by: str = ""
    reason: str = ""
    baseline_ack: bool = False
    deal_breaker_ack: bool = False


def _number(version_id: str) -> int:
    return int(version_id[1:]) if version_id[1:].isdigit() else 0


def problems(store: Store, suite_id: str, decision: Decision, events: list[ReleaseEvent], in_use_id: str,
             comp=None) -> list[str]:
    """Why this decision can't be recorded right now. Empty means it can.

    comp is the comparison for the evaluation relied on, rebuilt at submission with the
    overrides in force. Return decisions rely on no evaluation.
    """
    from . import evaluate

    out: list[str] = []
    reason = " ".join(decision.reason.split())
    if not decision.token:
        out.append("This form is out of date. Reload the page and try again.")
    if len(decision.confirmed_by) > MAX_NAME:
        out.append(f"Keep the name under {MAX_NAME} characters.")
    if decision.version_id == in_use_id:
        out.append(f"Version {_number(decision.version_id)} is already in use.")
        return out

    if decision.kind == "return":
        if status(decision.version_id, events, in_use_id) != "Retired":
            out.append("Only a version that was in use before can be put back into use.")
        if not reason:
            out.append("Add a one-line reason, so your team knows why you went back.")
        elif len(reason) > MAX_REASON:
            out.append(f"Keep the reason to one line, under {MAX_REASON} characters.")
        return out

    # 1 and 2: a saved, complete evaluation of this version
    record = comp.record if comp is not None else None
    if record is None:
        out.append("That test couldn't be found. Open the result again.")
        return out
    if record.source == "live" and not (record.id and store.load_evaluation(suite_id, record.id)):
        out.append("Test these two versions together first, so there's a saved result to rely on.")
        return out
    if record.status != "complete" or not comp.complete:
        out.append("This test is incomplete. Retry the missing examples before rolling out.")
        return out
    if record.candidate.id != decision.version_id:
        out.append(f"This test is for version {record.candidate.number}, not version {_number(decision.version_id)}.")
        return out
    # 3: the evidence still matches the current inputs
    if evaluate.is_stale(store, record):
        out.append(STALE)
        return out
    verdict = comp.verdict
    changed = record.baseline.id != in_use_id
    # 4: the baseline is still the version in use, or the owner acknowledged it isn't
    if changed and decision.kind == "rollout":
        out.append(changed_baseline_text(record.candidate.number, record.baseline.number, _number(in_use_id))
                   + f" Test version {record.candidate.number} against version {_number(in_use_id)}, "
                   "or roll it out anyway.")
        return out
    if changed and not decision.baseline_ack:
        out.append("Tick \"I understand this test didn't compare against the version in use.\" to roll out anyway.")
    if decision.kind == "rollout" and verdict != scoring.READY:
        out.append(f"This result is \"{verdict}\", so it can't go straight into use. Use Roll out anyway with a reason.")
    if decision.kind == "exception":
        if verdict == scoring.READY and not changed:
            out.append("This version is ready to roll out, so put it into use directly.")
        # 5: deal-breaker misses were shown and read
        misses = sum(1 for r in comp.rows if r.blocking_misses)
        if verdict == scoring.NOT_READY and misses and not decision.deal_breaker_ack:
            out.append(f"Tick \"I've read the {misses} {'example' if misses == 1 else 'examples'} that missed "
                       "a deal-breaker.\" to roll out anyway.")
        # 6: a reason
        if not reason:
            out.append("Add a one-line reason, so your team knows why it rolled out anyway.")
        elif len(reason) > MAX_REASON:
            out.append(f"Keep the reason to one line, under {MAX_REASON} characters.")
    return out


def decide(store: Store, suite_id: str, decision: Decision, log: ReleaseLog, comp=None,
           active_override_ids: list[str] | None = None) -> tuple[ReleaseEvent | None, list[str], bool]:
    """Check and record a decision. Returns (event, problems, created).

    The same token twice returns the release it already made (created False). If any
    check fails, nothing is recorded.
    """
    with log.lock():
        events = history(store, suite_id, log)
        if decision.token:
            done = next((e for e in events if e.token == decision.token), None)
            if done is not None:
                return done, [], False
        suite = store.load_suite(suite_id)
        current = in_use(events, [v.id for v in store.versions(suite_id)], suite.in_use)
        found = problems(store, suite_id, decision, events, current, comp)
        if found:
            return None, found, False
        record = comp.record if comp is not None and decision.kind != "return" else None
        event = ReleaseEvent(
            id=uuid.uuid4().hex[:12], type=decision.kind, from_version=current, to_version=decision.version_id,
            timestamp=now(), confirmed_by=decision.confirmed_by.strip(),
            evaluation_id=record.id if record else "", evaluation_source=record.source if record else "",
            verdict=comp.verdict if record else None,
            override_ids=list(active_override_ids or []) if record else [],
            reason=" ".join(decision.reason.split()) if decision.kind != "rollout" else "",
            baseline_ack=decision.baseline_ack and record is not None and record.baseline.id != current,
            token=decision.token,
        )
        log.append(event)
        return event, [], True


def baseline_event(version_id: str, confirmed_by: str = "") -> ReleaseEvent:
    """For a new set: the version the team uses today, recorded as in use by the owner."""
    return ReleaseEvent(id=uuid.uuid4().hex[:12], type="baseline", to_version=version_id, timestamp=now(),
                        confirmed_by=confirmed_by.strip()[:MAX_NAME])


def new_token() -> str:
    return uuid.uuid4().hex[:16]
