"""Settings read from the environment. The API key never leaves the server."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5-5"
MAX_TEXT_CHARS = 20_000
PUBLIC_MAX_TEXT_CHARS = 5_000
PUBLIC_MAX_EXAMPLES = 8

FEATURED_SUITE = "followup-email"
FEATURED_CURRENT = "v2"
FEATURED_NEW = "v3"
SAVED_EXAMPLE_VERSION = "v4"

PACKAGE_ROOT = Path(__file__).resolve().parent


def default_suites_dir() -> Path:
    env = os.environ.get("ROLLOUT_SUITES_DIR")
    if env:
        return Path(env)
    return PACKAGE_ROOT.parent / "suites"


def default_data_dir() -> Path:
    env = os.environ.get("ROLLOUT_DATA_DIR")
    return Path(env) if env else Path(tempfile.gettempdir()) / "rollout-ready"


@dataclass(frozen=True)
class Limits:
    """Caps on live runs when the app is open to anyone (ROLLOUT_PUBLIC)."""
    per_session: int = 3
    per_ip_per_day: int = 5
    per_day: int = 10


@dataclass(frozen=True)
class Settings:
    mode: str  # "demo" or "live", after any fallback
    requested_mode: str
    judge_model: str
    suites_dir: Path
    api_key: str | None = None
    public: bool = False  # ROLLOUT_PUBLIC: open to anyone, with private sessions and limits
    limits: Limits = field(default_factory=Limits)
    data_dir: Path = field(default_factory=default_data_dir)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_public(self) -> bool:
        """Public live mode. Without a key the app is in demo mode, which is already safe to share."""
        return self.public and self.is_live

    @property
    def max_chars(self) -> int:
        return PUBLIC_MAX_TEXT_CHARS if self.is_public else MAX_TEXT_CHARS

    @property
    def fallback_notice(self) -> str | None:
        if self.requested_mode == "live" and self.mode == "demo":
            return (
                "Live mode is turned on, but no Anthropic API key was found on the server, "
                "so the app is showing recorded demo runs instead."
            )
        return None

    def __repr__(self) -> str:  # keep the key out of logs and tracebacks
        return (
            f"Settings(mode={self.mode!r}, public={self.is_public!r}, judge_model={self.judge_model!r}, "
            f"suites_dir={str(self.suites_dir)!r}, api_key={'set' if self.api_key else 'missing'})"
        )


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _count(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def load_settings(suites_dir: Path | None = None, force_live: bool = False) -> Settings:
    requested = "live" if force_live else os.environ.get("ROLLOUT_MODE", "demo").strip().lower()
    if requested not in ("demo", "live"):
        requested = "demo"
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    mode = "live" if requested == "live" and key else "demo"
    return Settings(
        mode=mode,
        requested_mode=requested,
        judge_model=os.environ.get("ROLLOUT_JUDGE_MODEL", "").strip() or DEFAULT_JUDGE_MODEL,
        suites_dir=suites_dir or default_suites_dir(),
        api_key=key if mode == "live" else None,
        public=_flag("ROLLOUT_PUBLIC"),
        limits=Limits(
            per_session=_count("ROLLOUT_MAX_RUNS_PER_SESSION", Limits.per_session),
            per_ip_per_day=_count("ROLLOUT_MAX_RUNS_PER_IP_PER_DAY", Limits.per_ip_per_day),
            per_day=_count("ROLLOUT_MAX_RUNS_PER_DAY", Limits.per_day),
        ),
    )


def length_problem(text: str, what: str, limit: int = MAX_TEXT_CHARS) -> str | None:
    """Friendly message when pasted text is over the limit, else None."""
    if len(text) > limit:
        return (
            f"That {what} is {len(text):,} characters long. The limit is {limit:,}, "
            "so please trim it a little and try again."
        )
    return None
