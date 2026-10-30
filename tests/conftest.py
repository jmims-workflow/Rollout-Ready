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
    """Stands in for Claude in tests. Counts calls; never touches the network."""

    def __init__(self, score: int = 4, fail_after: int | None = None):
        self.score = score
        self.fail_after = fail_after
        self.generate_calls = 0
        self.judge_calls = 0

    def _maybe_fail(self):
        from rollout_ready.provider import ProviderError
        if self.fail_after is not None and self.generate_calls + self.judge_calls >= self.fail_after:
            raise ProviderError("Claude is busy right now (rate limit).")

    def generate(self, *, model, prompt, user_input):
        self._maybe_fail()
        self.generate_calls += 1
        return Generation(f"Reply on Friday about: {user_input[:40]}", 100, 50)

    def structured(self, *, model, system, user, schema):
        self._maybe_fail()
        if schema is JudgeReply:
            self.judge_calls += 1
            ids = re.findall(r"^Criterion id: (\S+)$", user, flags=re.M)
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
