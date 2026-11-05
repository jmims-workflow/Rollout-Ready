"""A small storage interface for records, so the backend can change later.

Every new record (answers, reviews, evaluations, releases, overrides, tokens)
goes through a RecordStore. The only backend today is LocalRecordStore, which
keeps JSON files in a folder:

- read(key)               the stored value, or None
- write_once(key, value)  stores a value that can never be replaced; False if it exists
- update(key, change)     read, change and write back with compare-and-swap, so no
                          update is silently lost
- list(prefix)            the keys stored under a prefix

Keys look like "evaluations/20261105-abc123" or "releases". Each part may use
letters, numbers, dots, dashes and underscores only.

The app runs as one instance, so a lock per file serializes writers and
compare-and-swap catches anything that changed underneath. set_lock() gives a lock per instruction set for
read-modify-write operations that touch more than one record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

KEY_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_CAS_ATTEMPTS = 50


class RecordError(Exception):
    """A record key or value is not allowed."""


class RecordConflict(Exception):
    """Another writer changed a record first and retries ran out."""


class RecordStore(Protocol):
    def read(self, key: str) -> Any | None: ...

    def write_once(self, key: str, value: Any) -> bool: ...

    def update(self, key: str, change: Callable[[Any | None], Any]) -> Any: ...

    def list(self, prefix: str = "") -> list[str]: ...


# locks ------------------------------------------------------------------------
_registry_lock = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}
_set_locks: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    with _registry_lock:
        return _path_locks.setdefault(str(path), threading.RLock())


def set_lock(name: str) -> threading.RLock:
    """One re-entrant lock per instruction set (pass a folder path or set id)."""
    with _registry_lock:
        return _set_locks.setdefault(str(name), threading.RLock())


# local folder backend -------------------------------------------------------------
def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _version(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


class LocalRecordStore:
    """JSON files under one folder. Writes go to a temporary file first, then rename."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        parts = key.split("/") if key else []
        if not parts or not all(KEY_PART_RE.fullmatch(p) for p in parts):
            raise RecordError(f"Not a valid record key: {key!r}")
        return self.root.joinpath(*parts[:-1], parts[-1] + ".json")

    def _raw(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _temp(self, path: Path, text: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return tmp

    def read(self, key: str) -> Any | None:
        raw = self._raw(self._path(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RecordError(f"{self._path(key)} could not be read: {exc}") from exc

    def write_once(self, key: str, value: Any) -> bool:
        """Store a value that can never be replaced. Returns False, changing nothing, if it exists."""
        path = self._path(key)
        tmp = self._temp(path, _dump(value))
        try:
            os.link(tmp, path)  # fails if the file exists, so a record is never overwritten
            return True
        except FileExistsError:
            return False
        finally:
            os.unlink(tmp)

    def version(self, key: str) -> str | None:
        """A fingerprint of the stored bytes, for compare-and-swap. None when missing."""
        return _version(self._raw(self._path(key)))

    def compare_and_swap(self, key: str, expected: str | None, value: Any) -> bool:
        """Write only if the record is still at the expected version. False if someone else wrote first."""
        path = self._path(key)
        with _path_lock(path):
            if _version(self._raw(path)) != expected:
                return False
            tmp = self._temp(path, _dump(value))
            os.replace(tmp, path)
            return True

    def update(self, key: str, change: Callable[[Any | None], Any]) -> Any:
        """Apply change() to the current value and store the result.

        Writers in this app queue on the file's lock, so none of them starve. The
        compare-and-swap still guards against a writer outside the lock, and the
        change is reapplied to the newer value if one got in first.
        """
        path = self._path(key)
        with _path_lock(path):
            for _ in range(MAX_CAS_ATTEMPTS):
                raw = self._raw(path)
                current = json.loads(raw) if raw is not None else None
                new = change(copy.deepcopy(current))
                if self.compare_and_swap(key, _version(raw), new):
                    return new
        raise RecordConflict(f"{path} kept changing while it was being saved. Try again.")

    def list(self, prefix: str = "") -> list[str]:
        folder = self.root.joinpath(*prefix.split("/")) if prefix else self.root
        if prefix and not all(KEY_PART_RE.fullmatch(p) for p in prefix.split("/")):
            raise RecordError(f"Not a valid record prefix: {prefix!r}")
        if not folder.is_dir():
            return []
        keys = []
        for p in sorted(folder.rglob("*.json")):
            if p.name.startswith(".tmp-"):
                continue
            rel = p.relative_to(self.root).with_suffix("")
            keys.append("/".join(rel.parts))
        return keys
