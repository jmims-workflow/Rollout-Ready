"""Public live mode (ROLLOUT_PUBLIC=true): private visitor folders, shared demo results, limits.

data_dir/
  sessions/<session-id>/<suite-id>/...       one visitor's prompts, versions, examples, results, overrides
  shared/<suite-id>/answers|reviews/<key>    real answers and reviews for demo versions on demo examples
  usage.json                                 live runs used, per session, per address and per day

A visitor reads the demo prompts from the suites folder. The first time they change
one, it is copied into their own folder, so nothing a visitor writes is ever seen by
anyone else. A real answer for an unchanged demo version on an unchanged demo example,
with the demo's model, contains nothing a visitor wrote, so it goes to the shared
folder and is reused by everyone; so does its review when the qualities are the demo's
own. Evaluation records are always private to the session.
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
from .evidence import AnswerBlob, EvaluationRecord, OverrideEvent, ReviewBlob, qualities_payload
from .models import Case, VersionResults
from .records import LocalRecordStore
from .storage import Store, SuiteError, _atomic_write

SESSION_SECONDS = 24 * 60 * 60
SESSION_ID_RE = re.compile(r"[a-f0-9]{32}")
HONEYPOT = "website"
PRIVATE_NOTE = ("Your tests are private to you and last until you close this browser or for 24 hours. "
                "Don't paste anything confidential.")
USED_UP = "Today's live tests are used up. Try again tomorrow, or explore the sample results."
OTHER_SITE = "This form can only be sent from Rollout Ready's own pages. Go back, reload the page and try again."

_lock = threading.RLock()


class SharedResults:
    """Real answers and reviews for demo versions on demo examples, shared by every visitor.

    Stored once under their reuse keys. SessionStore decides what may come here.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _records(self, suite_id: str) -> LocalRecordStore:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite_id or ""):
            raise SuiteError(f"There is no prompt called {suite_id!r}.")
        return LocalRecordStore(self.root / suite_id)

    def _read(self, suite_id: str, key: str, model):
        try:
            data = self._records(suite_id).read(key)
            return model.model_validate(data) if data is not None else None
        except (ValidationError, Exception):
            return None  # a broken cache file only means that call runs again

    def load_answer(self, suite_id: str, key: str) -> AnswerBlob | None:
        return self._read(suite_id, f"answers/{key}", AnswerBlob)

    def save_answer(self, suite_id: str, blob: AnswerBlob) -> None:
        self._records(suite_id).write_once(f"answers/{blob.key}", blob.model_dump(mode="json"))

    def load_review(self, suite_id: str, key: str) -> ReviewBlob | None:
        return self._read(suite_id, f"reviews/{key}", ReviewBlob)

    def save_review(self, suite_id: str, blob: ReviewBlob) -> None:
        self._records(suite_id).write_once(f"reviews/{blob.key}", blob.model_dump(mode="json"))


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
            if (src / "history.json").exists():
                shutil.copy2(src / "history.json", tmp / "history.json")
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

    def append_override_event(self, suite_id: str, event: OverrideEvent) -> None:
        self._copy_if_demo(suite_id)
        super().append_override_event(suite_id, event)

    def override_events(self, suite_id: str) -> list[OverrideEvent]:
        return super().override_events(suite_id) if self._own(suite_id) else []

    def append_release_event(self, suite_id: str, event: dict) -> None:
        self._copy_if_demo(suite_id)
        super().append_release_event(suite_id, event)

    def release_events(self, suite_id: str) -> list[dict]:
        return super().release_events(suite_id) if self._own(suite_id) else []

    # results: answers and reviews may be shared; evaluation records never are ----
    def _demo_version(self, suite_id: str, version_id: str) -> bool:
        """True when this version is one of the demo's own, unchanged."""
        try:
            return self.base.load_version(suite_id, version_id).text == self.load_version(suite_id, version_id).text
        except SuiteError:
            return False

    def _shareable_answer(self, suite_id: str, version_id: str, case: Case) -> bool:
        """An unchanged demo version on an unchanged demo example, with the demo's model."""
        if suite_id not in self.base.suite_ids() or not self._demo_version(suite_id, version_id):
            return False
        try:
            demo_case = next((c for c in self.base.load_cases(suite_id) if c.id == case.id), None)
            same_model = self.base.load_suite(suite_id).model == self.load_suite(suite_id).model
        except SuiteError:
            return False
        return demo_case is not None and demo_case.input == case.input and same_model

    def _shareable_review(self, suite_id: str, version_id: str, case: Case) -> bool:
        """A shareable answer, reviewed against the demo's own qualities."""
        if not self._shareable_answer(suite_id, version_id, case):
            return False
        try:
            return qualities_payload(self.base.load_suite(suite_id).rubric) == \
                qualities_payload(self.load_suite(suite_id).rubric)
        except SuiteError:
            return False

    def load_answer(self, suite_id: str, key: str) -> AnswerBlob | None:
        own = super().load_answer(suite_id, key) if (self.root / suite_id).exists() else None
        return own or self.shared.load_answer(suite_id, key)

    def save_answer(self, suite_id: str, blob: AnswerBlob, *, version_id: str, case: Case) -> None:
        if self._shareable_answer(suite_id, version_id, case):
            self.shared.save_answer(suite_id, blob)
            return
        self._copy_if_demo(suite_id)
        super().save_answer(suite_id, blob, version_id=version_id, case=case)

    def load_review(self, suite_id: str, key: str) -> ReviewBlob | None:
        own = super().load_review(suite_id, key) if (self.root / suite_id).exists() else None
        return own or self.shared.load_review(suite_id, key)

    def save_review(self, suite_id: str, blob: ReviewBlob, *, version_id: str, case: Case) -> None:
        if self._shareable_review(suite_id, version_id, case):
            self.shared.save_review(suite_id, blob)
            return
        self._copy_if_demo(suite_id)
        super().save_review(suite_id, blob, version_id=version_id, case=case)

    def evaluation_ids(self, suite_id: str) -> list[str]:
        return super().evaluation_ids(suite_id) if self._own(suite_id) else []

    def save_evaluation(self, record: EvaluationRecord) -> None:
        self._copy_if_demo(record.suite_id)
        super().save_evaluation(record)

    def load_recorded(self, suite_id: str, version_id: str) -> VersionResults | None:
        if not self._demo_version(suite_id, version_id):
            return None
        return self.base.load_recorded(suite_id, version_id)

    def load_runs(self, suite_id: str, version_id: str) -> VersionResults | None:
        # Older results only. Never read runs/ in the demo folder: those belong to whoever ran the app locally.
        return super().load_runs(suite_id, version_id) if self._own(suite_id) else None


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
