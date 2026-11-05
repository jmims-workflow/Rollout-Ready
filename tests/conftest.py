import re
import shutil
import socket
from pathlib import Path

import pytest

from rollout_ready.config import Settings
from rollout_ready.provider import Generation, JudgeReply, JudgedCriterion, Parsed
from rollout_ready.storage import Store

REPO = Path(__file__).resolve().parent.parent
SUITES = REPO / "suites"


class FakeProvider:
    """Stands in for Claude in tests. Counts calls; never touches the network.

    script: optional list of steps, one used per call (generate or review) before
    falling back to normal replies. A step is "ok", "cut_off", "unreadable",
    "missing_score", or a ProviderError to raise.
    """

    def __init__(self, score: int = 4, fail_after: int | None = None, script=None):
        self.score = score
        self.fail_after = fail_after
        self.script = list(script or [])
        self.generate_calls = 0  # calls that returned an answer
        self.judge_calls = 0  # calls that returned a review
        self.generate_attempts = 0  # every call, including failed ones
        self.judge_attempts = 0

    def _maybe_fail(self):
        from rollout_ready.provider import ProviderError
        if self.fail_after is not None and self.generate_calls + self.judge_calls >= self.fail_after:
            raise ProviderError("Claude is busy right now (rate limit).")

    def _step(self):
        from rollout_ready.provider import ProviderError
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, ProviderError):
            raise step
        return step

    def generate(self, *, model, prompt, user_input):
        self.generate_attempts += 1
        self._maybe_fail()
        step = self._step()
        self.generate_calls += 1
        if step == "cut_off":
            return Generation(f"Reply on Friday about: {user_input[:40]} and then it just stops", 100, 8000, "max_tokens")
        return Generation(f"Reply on Friday about: {user_input[:40]}", 100, 50)

    def structured(self, *, model, system, user, schema):
        from rollout_ready.provider import ProviderError
        if schema is JudgeReply:
            self.judge_attempts += 1
        self._maybe_fail()
        if schema is JudgeReply:
            step = self._step()
            if step == "unreadable":
                raise ProviderError("Claude's reply could not be read as scores. Try again.", kind="unreadable",
                                    input_tokens=300, output_tokens=20)
            if step == "cut_off":
                self.judge_calls += 1
                return Parsed(None, 300, 8000, "max_tokens")
            ids = re.findall(r"^Criterion id: (\S+)$", user, flags=re.M)
            if step == "missing_score":
                ids = ids[:-1]
            self.judge_calls += 1
            reply = JudgeReply(scores=[JudgedCriterion(criterion_id=i, score=self.score, reason="Fine.") for i in ids])
            return Parsed(reply, 300, 80)
        from rollout_ready.provider import DraftReply, DraftedCriterion
        return Parsed(DraftReply(criteria=[DraftedCriterion(name="Clear", five_looks_like="Very clear.", one_looks_like="Confusing.")]), 200, 60)


@pytest.fixture
def demo_store():
    return Store(SUITES)


@pytest.fixture
def suites_copy(tmp_path):
    dest = tmp_path / "suites"
    shutil.copytree(SUITES, dest, ignore=shutil.ignore_patterns("runs"))
    return dest


@pytest.fixture
def live_settings(suites_copy):
    return Settings(mode="live", requested_mode="live", judge_model="claude-sonnet-5-5",
                    suites_dir=suites_copy, api_key="test-key-not-real")


@pytest.fixture
def demo_settings():
    return Settings(mode="demo", requested_mode="demo", judge_model="claude-sonnet-5-5", suites_dir=SUITES)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("demo mode tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): p.stat().st_mtime_ns for p in folder.rglob("*") if p.is_file()}
