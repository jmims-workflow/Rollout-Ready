"""Public live mode (ROLLOUT_PUBLIC=true): private visitor folders, shared demo results, limits.

data_dir/
  sessions/<session-id>/<suite-id>/...  one visitor's prompts, versions, examples, results, overrides
  shared/<suite-id>/<version>.json      real results for demo versions on demo examples
  usage.json                            live runs used, per session, per address and per day

A visitor reads the demo prompts from the suites folder. The first time they change
one, it is copied into their own folder, so nothing a visitor writes is ever seen by
anyone else. Real results for a demo version on an unchanged demo example contain
nothing a visitor wrote, so they go to the shared folder and are reused by everyone.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from .config import Limits
from .models import CaseResult, Override, Suite, VersionResults
from .storage import Store, SuiteError, _atomic_write, input_hash, rubric_hash

SESSION_SECONDS = 24 * 60 * 60
SESSION_ID_RE = re.compile(r"[a-f0-9]{32}")
HONEYPOT = "website"
PRIVATE_NOTE = ("Your tests are private to you and last until you close this browser or for 24 hours. "
                "Don't paste anything confidential.")
USED_UP = "Today's live tests are used up. Try again tomorrow, or explore the sample results."
OTHER_SITE = "This form can only be sent from Rollout Ready's own pages. Go back, reload the page and try again."

_lock = threading.RLock()


class SharedResults:
    """Real results for demo versions on demo examples, shared by every visitor."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, suite_id: str, version_id: str) -> Path:
        return self.root / suite_id / f"{version_id}.json"

    def load(self, suite_id: str, version_id: str) -> VersionResults | None:
        path = self._path(suite_id, version_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["source"] = "live"
            return VersionResults.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            return None  # a broken cache file only means those examples run again

    def save(self, suite_id: str, results: VersionResults, entries: dict[str, CaseResult]) -> None:
        with _lock:
            saved = self.load(suite_id, results.version) or VersionResults(version=results.version)
            saved.model, saved.judge_model = results.model, results.judge_model
            saved.ran_at = max(saved.ran_at, results.ran_at)
            saved.cases.update(entries)
            _atomic_write(self._path(suite_id, results.version),
                          saved.model_dump_json(indent=2, exclude={"source"}) + "\n")


class SessionStore(Store):
    """One visitor's view: the demo prompts plus anything they wrote, which only they can see."""

    def __init__(self, root: Path, base: Store, shared: SharedResults):
        super().__init__(root)
        self.base, self.shared = base, shared

    def _own(self, suite_id: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite_id or "")) and (self.root / suite_id / "suite.yaml").exists()

    def _copy(self, suite_id: str) -> None:
        """Copy a demo prompt into the visitor's folder before they change it. Results are not copied."""
        if self._own(suite_id):
            return
        src = self.base.suite_dir(suite_id)
        dest = self.root / suite_id
        with _lock:
            if self._own(suite_id):
                return
            tmp = self.root / f".copy-{suite_id}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            shutil.copy2(src / "cases.yaml", tmp / "cases.yaml")
            shutil.copytree(src / "versions", tmp / "versions")
            shutil.copy2(src / "suite.yaml", tmp / "suite.yaml")
            tmp.rename(dest)

    # reading: the visitor's own copy first, then the demo prompts ----------
    def suite_ids(self) -> list[str]:
        return sorted(set(super().suite_ids()) | set(self.base.suite_ids()))

    def suite_dir(self, suite_id: str) -> Path:
        if self._own(suite_id):
            return self.root / suite_id
        return self.base.suite_dir(suite_id)

    def _id_taken(self, suite_id: str) -> bool:
        return super()._id_taken(suite_id) or suite_id in self.base.suite_ids()

    # writing: always into the visitor's folder ------------------------------
    def _copy_if_demo(self, suite_id: str) -> None:
        if suite_id in self.base.suite_ids():
            self._copy(suite_id)

    def save_cases(self, suite_id, cases) -> None:
        self._copy_if_demo(suite_id)
        super().save_cases(suite_id, cases)

    def add_version(self, suite_id, text, note, created=None):
        self._copy_if_demo(suite_id)
        return super().add_version(suite_id, text, note, created)

    def save_override(self, suite_id: str, current: str, new: str, case_id: str, override: Override) -> None:
        self._copy(suite_id)
        super().save_override(suite_id, current, new, case_id, override)

    def remove_override(self, suite_id: str, current: str, new: str, case_id: str) -> None:
        if self._own(suite_id):
            super().remove_override(suite_id, current, new, case_id)

    def _all_overrides(self, suite_id: str) -> dict:
        return super()._all_overrides(suite_id) if self._own(suite_id) else {}

    # results ----------------------------------------------------------------
    def _demo_version(self, suite_id: str, version_id: str) -> bool:
        """True when this version is one of the demo's own, unchanged."""
        try:
            return self.base.load_version(suite_id, version_id).text == self.load_version(suite_id, version_id).text
        except SuiteError:
            return False

    def _shareable(self, suite_id: str) -> tuple[Suite, dict[str, str]] | None:
        """The demo prompt and the input of each demo example this visitor has not changed."""
        try:
            base_suite = self.base.load_suite(suite_id)
            if rubric_hash(base_suite) != rubric_hash(self.load_suite(suite_id)):
                return None
            mine = {c.id: input_hash(c) for c in self.load_cases(suite_id)}
            demo = {c.id: input_hash(c) for c in self.base.load_cases(suite_id)}
        except SuiteError:
            return None
        return base_suite, {cid: h for cid, h in demo.items() if mine.get(cid) == h}

    def _shared_entries(self, suite_id: str, version_id: str, entries: dict[str, CaseResult]) -> dict[str, CaseResult]:
        if not entries or not self._demo_version(suite_id, version_id):
            return {}
        found = self._shareable(suite_id)
        if not found:
            return {}
        suite, inputs = found
        return {cid: e for cid, e in entries.items()
                if cid in inputs and e.input_hash == inputs[cid]
                and e.rubric_hash == rubric_hash(suite) and e.model == suite.model}

    def load_recorded(self, suite_id: str, version_id: str) -> VersionResults | None:
        if not self._demo_version(suite_id, version_id):
            return None
        return self.base.load_recorded(suite_id, version_id)

    def load_runs(self, suite_id: str, version_id: str) -> VersionResults | None:
        # Never read runs/ in the demo folder itself: those belong to whoever ran the app locally.
        own = super().load_runs(suite_id, version_id) if self._own(suite_id) else None
        shared = self.shared.load(suite_id, version_id)
        common = self._shared_entries(suite_id, version_id, shared.cases) if shared else {}
        if own is None and not common:
            return None
        out = own or VersionResults(version=version_id, source="live")
        if common:
            out.cases.update(common)
            out.model = out.model or shared.model
            out.judge_model = out.judge_model or shared.judge_model
            out.ran_at = max(out.ran_at, shared.ran_at)
        return out

    def save_runs(self, suite_id: str, results: VersionResults) -> None:
        common = self._shared_entries(suite_id, results.version, results.cases)
        if common:
            self.shared.save(suite_id, results, common)
        private = {cid: e for cid, e in results.cases.items() if cid not in common}
        if not private and not self._own(suite_id):
            return
        self._copy_if_demo(suite_id)
        super().save_runs(suite_id, results.model_copy(update={"cases": private}))


class Sessions:
    """Folders for each visitor, removed once they are more than a day old."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def folder(self, session_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("not a session id")
        return self.root / session_id

    def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        if not self.root.exists():
            return
        for p in self.root.iterdir():
            try:
                if p.is_dir() and now - p.stat().st_mtime > SESSION_SECONDS:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue


class Usage:
    """Counts live runs per session, per address per day and across everyone per day.

    Addresses are stored only as salted hashes. Counts are kept in a file so a
    restart of the same instance does not reset them.
    """

    def __init__(self, path: Path, limits: Limits, salt: str, clock=time.time):
        self.path, self.limits, self.salt, self.clock = Path(path), limits, salt, clock

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

    def _read(self) -> dict:
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        if data.get("day") != self._today():
            data = {"day": self._today(), "total": 0, "addresses": {}, "sessions": data.get("sessions", {})}
        now = self.clock()
        data["sessions"] = {k: v for k, v in data.get("sessions", {}).items()
                            if now - v.get("started", 0) <= SESSION_SECONDS}
        return data

    def _address(self, ip: str) -> str:
        return hashlib.sha256(f"{self.salt}:{ip}".encode()).hexdigest()[:20]

    def _left(self, data: dict, session_id: str, ip: str) -> int:
        lim = self.limits
        used_session = data["sessions"].get(session_id, {}).get("runs", 0)
        used_ip = data["addresses"].get(self._address(ip), 0)
        return max(0, min(lim.per_session - used_session, lim.per_ip_per_day - used_ip, lim.per_day - data["total"]))

    def left(self, session_id: str, ip: str) -> int:
        with _lock:
            return self._left(self._read(), session_id, ip)

    def take(self, session_id: str, ip: str) -> bool:
        """Use one live run. False, and nothing used, when any limit is reached."""
        with _lock:
            data = self._read()
            if self._left(data, session_id, ip) <= 0:
                return False
            data["total"] += 1
            addr = self._address(ip)
            data["addresses"][addr] = data["addresses"].get(addr, 0) + 1
            entry = data["sessions"].setdefault(session_id, {"runs": 0, "started": self.clock()})
            entry["runs"] += 1
            _atomic_write(self.path, json.dumps(data, indent=2) + "\n")
            return True


def client_ip(headers, peer: str | None) -> str:
    """The visitor's address. On Cloud Run that is the first one in X-Forwarded-For."""
    forwarded = headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or peer or "unknown"


def same_origin(headers) -> bool:
    """True when a form post came from one of the app's own pages."""
    source = headers.get("origin") or headers.get("referer") or ""
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip().lower()
    return bool(host) and urlparse(source).netloc.lower() == host
