"""Suite files on disk are the single source of truth.

suites/<suite-id>/
  suite.yaml, cases.yaml
  versions/v1.md ...          front matter (note, created) then the prompt text
  recorded/<version>.json     demo outputs and judge scores
  runs/<version>.json         live results
  runs/overrides.json         live overrides, keyed "<current>:<new>" then case id
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

from .models import Case, Override, Suite, Version, VersionResults

VERSION_RE = re.compile(r"^v(\d+)\.md$")


class SuiteError(Exception):
    """A suite file is missing or has a problem a person can fix."""


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

    def suites(self) -> list[Suite]:
        return [self.load_suite(s) for s in self.suite_ids()]

    def create_suite(self, suite: Suite, cases: list[Case], prompt_text: str, note: str = "Original") -> str:
        base = slugify(suite.name)
        suite_id, n = base, 2
        while (self.root / suite_id).exists():
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

    # overrides (live mode) --------------------------------------------
    def _overrides_path(self, suite_id: str) -> Path:
        return self.suite_dir(suite_id) / "runs" / "overrides.json"

    def _all_overrides(self, suite_id: str) -> dict:
        path = self._overrides_path(suite_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SuiteError(f"{path} could not be read: {exc}") from exc

    def load_overrides(self, suite_id: str, current: str, new: str) -> dict[str, Override]:
        pair = self._all_overrides(suite_id).get(pair_key(current, new), {})
        return {cid: Override.model_validate(o) for cid, o in pair.items()}

    def save_override(self, suite_id: str, current: str, new: str, case_id: str, override: Override) -> None:
        data = self._all_overrides(suite_id)
        data.setdefault(pair_key(current, new), {})[case_id] = override.model_dump()
        _atomic_write(self._overrides_path(suite_id), json.dumps(data, indent=2) + "\n")

    def remove_override(self, suite_id: str, current: str, new: str, case_id: str) -> None:
        data = self._all_overrides(suite_id)
        pair = data.get(pair_key(current, new), {})
        if pair.pop(case_id, None) is not None:
            if not pair:
                data.pop(pair_key(current, new))
            _atomic_write(self._overrides_path(suite_id), json.dumps(data, indent=2) + "\n")


def pair_key(current: str, new: str) -> str:
    return f"{current}:{new}"


def rubric_hash(suite: Suite) -> str:
    # Marking a deal-breaker changes no score, so it does not make saved results stale.
    payload = json.dumps([c.model_dump(exclude={"deal_breaker"}) for c in suite.rubric], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def input_hash(case: Case) -> str:
    return hashlib.sha256(case.input.encode()).hexdigest()[:12]
