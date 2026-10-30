"""Settings read from the environment. The API key never leaves the server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5-5"
MAX_TEXT_CHARS = 20_000

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


@dataclass(frozen=True)
class Settings:
    mode: str  # "demo" or "live", after any fallback
    requested_mode: str
    judge_model: str
    suites_dir: Path
    api_key: str | None = None

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

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
            f"Settings(mode={self.mode!r}, judge_model={self.judge_model!r}, "
            f"suites_dir={str(self.suites_dir)!r}, api_key={'set' if self.api_key else 'missing'})"
        )


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
    )


def length_problem(text: str, what: str) -> str | None:
    """Friendly message when pasted text is over the limit, else None."""
    if len(text) > MAX_TEXT_CHARS:
        return (
            f"That {what} is {len(text):,} characters long. The limit is {MAX_TEXT_CHARS:,}, "
            "so please trim it a little and try again."
        )
    return None
