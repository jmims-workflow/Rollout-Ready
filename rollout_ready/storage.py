"""Suite files on disk are the single source of truth.

suites/<suite-id>/
  suite.yaml, cases.yaml
  versions/v1.md ...          front matter (note, created) then the prompt text
  recorded/<version>.json     demo outputs and judge scores
  runs/<version>.json         live results
  history.json                release history that ships with the app (demo sets)
  runs/releases.json          release events added since, append-only
  runs/override-events.json   live overrides: append-only, keyed by evidence fingerprint and example
  runs/<key>.json             records written through the storage interface (records.py)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from .evidence import AnswerBlob, EvaluationRecord, OverrideEvent, ReviewBlob
from .models import Case, Suite, Version, VersionResults
from .records import LocalRecordStore, set_lock

VERSION_RE = re.compile(r"^v(\d+)\.md$")
OVERRIDE_EVENTS = "override-events"  # runs/override-events.json
RELEASE_EVENTS = "releases"  # runs/releases.json, after the shipped history.json


class SuiteError(Exception):
    """A suite file is missing or has a problem a person can fix."""


class FinalRecordError(Exception):
    """Someone tried to change an evaluation record that is already final. Final records never change."""


def _friendly(path: Path, err: ValidationError) -> SuiteError:
    lines = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "file"
        msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {msg}")
    return SuiteError(f"{path} has a problem. " + "; ".join(lines))


def _read_yaml(path: Path):
    if not path.exists():
        raise SuiteError(f"{path} is missing.")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SuiteError(f"{path} is not valid YAML: {exc}") from exc


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].strip("-") or "item"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    # records (evidence, releases, overrides, tokens) ---------------------
    def records(self, suite_id: str) -> LocalRecordStore:
        """Where this store writes records for one set: <root>/<suite-id>/runs/."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite_id or ""):
            raise SuiteError(f"There is no prompt called {suite_id!r}.")
        return LocalRecordStore(self.root / suite_id / "runs")

    def lock(self, suite_id: str):
        """A lock around read-modify-write operations on one set's records."""
        return set_lock(self.root / suite_id)

    # answers and reviews: stored once, under their reuse keys (evidence.py) -----
    def load_answer(self, suite_id: str, key: str) -> AnswerBlob | None:
        data = self.records(suite_id).read(f"answers/{key}")
        return AnswerBlob.model_validate(data) if data is not None else None

    def save_answer(self, suite_id: str, blob: AnswerBlob, *, version_id: str, case: Case) -> None:
        """Write once. version_id and case let a public session decide where it may go."""
        self.records(suite_id).write_once(f"answers/{blob.key}", blob.model_dump(mode="json"))

    def load_review(self, suite_id: str, key: str) -> ReviewBlob | None:
        data = self.records(suite_id).read(f"reviews/{key}")
        return ReviewBlob.model_validate(data) if data is not None else None

    def save_review(self, suite_id: str, blob: ReviewBlob, *, version_id: str, case: Case) -> None:
        self.records(suite_id).write_once(f"reviews/{blob.key}", blob.model_dump(mode="json"))

    # evaluation records -----------------------------------------------------
    def evaluation_ids(self, suite_id: str) -> list[str]:
        """Oldest first: ids start with the time the test started."""
        return [k.split("/", 1)[1] for k in self.records(suite_id).list("evaluations")]

    def load_evaluation(self, suite_id: str, evaluation_id: str) -> EvaluationRecord | None:
        data = self.records(suite_id).read(f"evaluations/{evaluation_id}")
        return EvaluationRecord.model_validate(data) if data is not None else None

    def save_evaluation(self, record: EvaluationRecord) -> None:
        """Write a new record. Its id must be new."""
        if not self.records(record.suite_id).write_once(f"evaluations/{record.id}", record.model_dump(mode="json")):
            raise FinalRecordError(f"An evaluation called {record.id!r} already exists.")

    def finish_evaluation(self, record: EvaluationRecord) -> None:
        """The only update a record ever gets: from running to final (complete, incomplete or interrupted).

        Refused, changing nothing, if the stored record is already final or the new one isn't.
        """
        if not record.final:
            raise FinalRecordError("A record can only be updated to a final status.")

        def change(current):
            if current is None:
                raise FinalRecordError(f"There is no evaluation called {record.id!r}.")
            if current.get("status") != "running":
                raise FinalRecordError(f"Evaluation {record.id!r} is final and can't be changed.")
            return record.model_dump(mode="json")

        self.records(record.suite_id).update(f"evaluations/{record.id}", change)

    # suites -----------------------------------------------------------
    def suite_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "suite.yaml").exists())

    def suite_dir(self, suite_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite_id or ""):
            raise SuiteError(f"There is no prompt called {suite_id!r}.")
        path = self.root / suite_id
        if not (path / "suite.yaml").exists():
            raise SuiteError(f"There is no prompt called {suite_id!r}.")
        return path

    def load_suite(self, suite_id: str) -> Suite:
        path = self.suite_dir(suite_id) / "suite.yaml"
        data = _read_yaml(path)
        if not isinstance(data, dict):
            raise SuiteError(f"{path} should be a list of settings like 'name:' and 'rubric:'.")
        try:
            suite = Suite.model_validate({**data, "id": suite_id})
        except ValidationError as err:
            raise _friendly(path, err) from err
        return suite

    def _id_taken(self, suite_id: str) -> bool:
        return (self.root / suite_id).exists()

    def suites(self) -> list[Suite]:
        return [self.load_suite(s) for s in self.suite_ids()]

    def create_suite(self, suite: Suite, cases: list[Case], prompt_text: str, note: str = "Original") -> str:
        base = slugify(suite.name)
        suite_id, n = base, 2
        while self._id_taken(suite_id):
            suite_id, n = f"{base}-{n}", n + 1
        folder = self.root / suite_id
        data = suite.model_dump(exclude={"id"})
        _atomic_write(folder / "suite.yaml", yaml.safe_dump(data, sort_keys=False, allow_unicode=False))
        self.save_cases(suite_id, cases)
        self.add_version(suite_id, prompt_text, note)
        return suite_id

    # cases ------------------------------------------------------------
    def load_cases(self, suite_id: str) -> list[Case]:
        path = self.suite_dir(suite_id) / "cases.yaml"
        data = _read_yaml(path) or []
        if not isinstance(data, list):
            raise SuiteError(f"{path} should be a list of examples.")
        cases = []
        for i, item in enumerate(data, start=1):
            try:
                cases.append(Case.model_validate(item))
            except ValidationError as err:
                raise SuiteError(f"{path}, example {i}: " + str(_friendly(path, err)).split("problem. ", 1)[1]) from err
        ids = [c.id for c in cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise SuiteError(f"{path}: each example needs its own id; repeated: {', '.join(dupes)}")
        return cases

    def save_cases(self, suite_id: str, cases: list[Case]) -> None:
        path = self.root / suite_id / "cases.yaml"
        data = [c.model_dump() for c in cases]
        _atomic_write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=100))

    def add_case(self, suite_id: str, name: str, label: str, input_text: str, kind: str = "everyday") -> Case:
        cases = self.load_cases(suite_id)
        taken = {c.id for c in cases}
        base = slugify(f"{label} {name}" if label else name)
        case_id, n = base, 2
        while case_id in taken:
            case_id, n = f"{base}-{n}", n + 1
        case = Case(id=case_id, name=name.strip(), label=label.strip(), kind=kind, input=input_text.strip())
        self.save_cases(suite_id, [*cases, case])
        return case

    def update_case(self, suite_id: str, case_id: str, name: str, label: str, input_text: str,
                    kind: str = "everyday") -> Case:
        cases = self.load_cases(suite_id)
        for i, c in enumerate(cases):
            if c.id == case_id:
                cases[i] = Case(id=case_id, name=name.strip(), label=label.strip(), kind=kind, input=input_text.strip())
                self.save_cases(suite_id, cases)
                return cases[i]
        raise SuiteError(f"There is no example called {case_id!r} in this prompt.")

    # versions ---------------------------------------------------------
    def version_numbers(self, suite_id: str) -> list[int]:
        folder = self.suite_dir(suite_id) / "versions"
        if not folder.exists():
            return []
        nums = [int(m.group(1)) for p in folder.iterdir() if (m := VERSION_RE.match(p.name))]
        return sorted(nums)

    def load_version(self, suite_id: str, version_id: str) -> Version:
        m = re.fullmatch(r"v(\d+)", version_id or "")
        path = self.suite_dir(suite_id) / "versions" / f"{version_id}.md"
        if not m or not path.exists():
            raise SuiteError(f"This prompt has no version called {version_id!r}.")
        raw = path.read_text(encoding="utf-8")
        meta: dict = {}
        body = raw
        if raw.startswith("---\n"):
            end = raw.find("\n---\n", 4)
            if end == -1:
                raise SuiteError(f"{path}: the front matter at the top is not closed with '---'.")
            meta = yaml.safe_load(raw[4:end]) or {}
            body = raw[end + 5 :]
        return Version(
            id=version_id,
            number=int(m.group(1)),
            title=str(meta.get("title", "") or "").strip(),
            note=str(meta.get("note", "")).strip(),
            created=str(meta.get("created", "")),
            text=body.strip("\n"),
        )

    def versions(self, suite_id: str) -> list[Version]:
        return [self.load_version(suite_id, f"v{n}") for n in self.version_numbers(suite_id)]

    def add_version(self, suite_id: str, text: str, note: str, created: str | None = None) -> Version:
        """Save a new version with the next number. Never overwrites an existing file."""
        folder = self.root / suite_id / "versions"
        folder.mkdir(parents=True, exist_ok=True)
        nums = [int(m.group(1)) for p in folder.iterdir() if (m := VERSION_RE.match(p.name))]
        number = max(nums, default=0) + 1
        meta = yaml.safe_dump({"note": note.strip(), "created": created or date.today().isoformat()},
                              sort_keys=False, allow_unicode=False).strip()
        content = f"---\n{meta}\n---\n{text.strip()}\n"
        while True:
            path = folder / f"v{number}.md"
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    fh.write(content)
                break
            except FileExistsError:
                number += 1
        return self.load_version(suite_id, f"v{number}")

    # results ----------------------------------------------------------
    def _results_path(self, suite_id: str, kind: str, version_id: str) -> Path:
        return self.suite_dir(suite_id) / kind / f"{version_id}.json"

    def load_recorded(self, suite_id: str, version_id: str) -> VersionResults | None:
        return self._load_results(self._results_path(suite_id, "recorded", version_id), "recorded")

    def load_runs(self, suite_id: str, version_id: str) -> VersionResults | None:
        return self._load_results(self._results_path(suite_id, "runs", version_id), "live")

    def _load_results(self, path: Path, source: str) -> VersionResults | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["source"] = source
            return VersionResults.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SuiteError(f"{path} could not be read: {exc}") from exc

    def save_runs(self, suite_id: str, results: VersionResults) -> None:
        path = self._results_path(suite_id, "runs", results.version)
        _atomic_write(path, results.model_dump_json(indent=2, exclude={"source"}) + "\n")

    # releases: events added since the shipped history (releases.py) --------------
    def release_events(self, suite_id: str) -> list[dict]:
        return self.records(suite_id).read(RELEASE_EVENTS) or []

    def append_release_event(self, suite_id: str, event: dict) -> None:
        with self.lock(suite_id):
            self.records(suite_id).update(RELEASE_EVENTS, lambda cur: [*(cur or []), event])

    # overrides: append-only events, keyed by evidence fingerprint and example ----
    def override_events(self, suite_id: str) -> list[OverrideEvent]:
        """Every override and removal ever made for this set, oldest first. Never edited or deleted."""
        return [OverrideEvent.model_validate(e) for e in self.records(suite_id).read(OVERRIDE_EVENTS) or []]

    def append_override_event(self, suite_id: str, event: OverrideEvent) -> None:
        with self.lock(suite_id):
            self.records(suite_id).update(OVERRIDE_EVENTS, lambda cur: [*(cur or []), event.model_dump(mode="json")])


def rubric_hash(suite: Suite) -> str:
    # Marking a deal-breaker changes no score, so it does not make saved results stale.
    payload = json.dumps([c.model_dump(exclude={"deal_breaker"}) for c in suite.rubric], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def input_hash(case: Case) -> str:
    return hashlib.sha256(case.input.encode()).hexdigest()[:12]
