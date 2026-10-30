from typer.testing import CliRunner

from conftest import SUITES, snapshot
from rollout_ready.cli import app

runner = CliRunner()


def test_list():
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 0 and "followup-email" in r.output and "v4  Answer first, then stay short" in r.output


def test_featured_run_exits_1(tmp_path, no_network, monkeypatch):
    monkeypatch.delenv("ROLLOUT_MODE", raising=False)
    before = snapshot(SUITES)
    report = tmp_path / "summary.md"
    r = runner.invoke(app, ["run", "followup-email", "--current", "v2", "--new", "v3", "--report", str(report)])
    assert r.exit_code == 1
    assert "Verdict: Not ready to roll out" in r.output
    assert "Worse: 2  Better: 4  No clear change: 2" in r.output
    assert "**Verdict: Not ready to roll out**" in report.read_text()
    assert snapshot(SUITES) == before


def test_ready_run_exits_0(no_network):
    r = runner.invoke(app, ["run", "followup-email", "--current", "v2", "--new", "v4"])
    assert r.exit_code == 0 and "Ready to roll out" in r.output


def test_keep_current_exits_1_and_no_difference_exits_0(no_network):
    assert runner.invoke(app, ["run", "release-notes", "--current", "v2", "--new", "v3"]).exit_code == 1
    assert runner.invoke(app, ["run", "release-notes", "--current", "v1", "--new", "v2"]).exit_code == 0


def test_live_without_key_falls_back(no_network, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = runner.invoke(app, ["run", "followup-email", "--current", "v2", "--new", "v4", "--live"])
    assert r.exit_code == 0 and "no Anthropic API key" in r.output


def test_unknown_version():
    r = runner.invoke(app, ["run", "followup-email", "--current", "v2", "--new", "v9"])
    assert r.exit_code == 2
