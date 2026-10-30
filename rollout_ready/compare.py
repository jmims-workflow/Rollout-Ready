"""Build a comparison between two versions from their saved results.

Demo and live mode both come through here, so the checks, scoring and
verdict code is exactly the same whichever mode produced the outputs.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from statistics import mean

from . import scoring
from .checks import CheckResult, run_checks, word_count
from .models import Case, CaseResult, Criterion, Run, Suite, Version, VersionResults
from .storage import Store, input_hash, rubric_hash


# results lookup ------------------------------------------------------------
def entry_is_current(entry: CaseResult, suite: Suite, case: Case) -> bool:
    """A saved result counts only if it was made with this model, rubric and input."""
    if entry.model and entry.model != suite.model:
        return False
    if entry.rubric_hash and entry.rubric_hash != rubric_hash(suite):
        return False
    if entry.input_hash and entry.input_hash != input_hash(case):
        return False
    return bool(entry.runs)


def load_results(store: Store, suite_id: str, version_id: str, live: bool) -> VersionResults:
    """Demo mode reads recorded results only. Live mode also reads its own saved runs."""
    recorded = store.load_recorded(suite_id, version_id)
    merged = VersionResults(version=version_id, source="recorded")
    if recorded:
        merged = recorded.model_copy(deep=True)
    if live:
        runs = store.load_runs(suite_id, version_id)
        if runs:
            merged.cases.update(runs.cases)
            merged.source = "live"
            merged.ran_at = runs.ran_at or merged.ran_at
            merged.model = runs.model or merged.model
            merged.judge_model = runs.judge_model or merged.judge_model
    return merged


def usable_runs(results: VersionResults, suite: Suite, case: Case) -> list[Run] | None:
    entry = results.cases.get(case.id)
    if entry and entry_is_current(entry, suite, case):
        return entry.runs[: suite.repeats] if len(entry.runs) >= suite.repeats else None
    return None


def missing_cases(store: Store, suite: Suite, cases: list[Case], version_id: str, live: bool) -> list[Case]:
    results = load_results(store, suite.id, version_id, live)
    return [c for c in cases if usable_runs(results, suite, c) is None]


# comparison ---------------------------------------------------------------
@dataclass
class CriterionRow:
    criterion: Criterion
    current: float
    new: float
    current_reason: str
    new_reason: str

    @property
    def drop(self) -> float:
        return round(self.current - self.new, 2)


@dataclass
class CheckRow:
    label: str
    current: CheckResult
    new: CheckResult


@dataclass
class CaseRow:
    case: Case
    current_score: float
    new_score: float
    change: float
    result: str
    current_output: str
    new_output: str
    criteria: list[CriterionRow]
    checks: list[CheckRow]

    @property
    def result_label(self) -> str:
        return scoring.RESULT_LABELS[self.result]

    @property
    def current_words(self) -> int:
        return word_count(self.current_output)

    @property
    def new_words(self) -> int:
        return word_count(self.new_output)

    @property
    def biggest_drop(self) -> CriterionRow | None:
        drops = [c for c in self.criteria if c.drop > 0]
        return max(drops, key=lambda c: c.drop) if drops else None

    @property
    def biggest_gain(self) -> CriterionRow | None:
        gains = [c for c in self.criteria if c.drop < 0]
        return min(gains, key=lambda c: c.drop) if gains else None

    @property
    def broken_checks(self) -> list[CheckRow]:
        return [c for c in self.checks if c.current.passed and not c.new.passed]

    @property
    def fixed_checks(self) -> list[CheckRow]:
        return [c for c in self.checks if c.new.passed and not c.current.passed]

    @property
    def checks_summary(self) -> str:
        if not self.checks:
            return "No automatic checks"
        parts = [f"{c.label}: now fails" for c in self.broken_checks]
        parts += [f"{c.label}: now passes" for c in self.fixed_checks]
        if parts:
            return "; ".join(parts)
        failing = [c.label for c in self.checks if not c.new.passed]
        if failing:
            return "Still failing: " + ", ".join(failing)
        return f"All {len(self.checks)} pass"

    @property
    def headline(self) -> str:
        """One sentence naming the biggest drop (or what changed most)."""
        drop = self.biggest_drop
        if self.broken_checks and (drop is None or self.result == scoring.WORSE and drop.drop < 1):
            return f"The new version fails a check the current one passed: {self.broken_checks[0].label.lower()}."
        if drop:
            return (
                f"The biggest drop is {drop.criterion.name}, from "
                f"{scoring.fmt_criterion(drop.current)} to {scoring.fmt_criterion(drop.new)}."
            )
        gain = self.biggest_gain
        if gain:
            return (
                f"No criterion dropped. The biggest gain is {gain.criterion.name}, from "
                f"{scoring.fmt_criterion(gain.current)} to {scoring.fmt_criterion(gain.new)}."
            )
        return "Every criterion scored the same in both versions."


@dataclass
class RunInfo:
    source: str
    model: str
    judge_model: str
    ran_at: str
    input_tokens: int
    output_tokens: int


@dataclass
class Comparison:
    suite: Suite
    current: Version
    new: Version
    cases: list[Case]
    rows: list[CaseRow] = field(default_factory=list)
    missing: dict[str, list[Case]] = field(default_factory=dict)
    current_info: RunInfo | None = None
    new_info: RunInfo | None = None

    @property
    def complete(self) -> bool:
        return not any(self.missing.values()) and bool(self.rows)

    def count(self, result: str) -> int:
        return sum(1 for r in self.rows if r.result == result)

    @property
    def worse(self) -> int:
        return self.count(scoring.WORSE)

    @property
    def better(self) -> int:
        return self.count(scoring.BETTER)

    @property
    def same(self) -> int:
        return self.count(scoring.SAME)

    @property
    def avg_current(self) -> float:
        return mean(r.current_score for r in self.rows)

    @property
    def avg_new(self) -> float:
        return mean(r.new_score for r in self.rows)

    @property
    def verdict(self) -> str:
        return scoring.verdict(self.worse, self.better)

    @property
    def verdict_key(self) -> str:
        return scoring.VERDICT_KEYS[self.verdict]

    @property
    def explanation(self) -> str:
        return scoring.explanation(
            self.verdict, self.new.full_name, self.current.full_name,
            self.worse, self.better, self.avg_current, self.avg_new,
        )

    @property
    def worse_rows(self) -> list[CaseRow]:
        return [r for r in self.rows if r.result == scoring.WORSE]

    def row(self, case_id: str) -> CaseRow | None:
        return next((r for r in self.rows if r.case.id == case_id), None)

    def diff(self) -> list[tuple[str, str]]:
        return prompt_diff(self.current.text, self.new.text)


def _info(results: VersionResults, rows_runs: list[list[Run]]) -> RunInfo:
    runs = [r for rs in rows_runs for r in rs]
    return RunInfo(
        source=results.source,
        model=results.model,
        judge_model=results.judge_model,
        ran_at=results.ran_at,
        input_tokens=sum(r.input_tokens + r.judge_input_tokens for r in runs),
        output_tokens=sum(r.output_tokens + r.judge_output_tokens for r in runs),
    )


def _checks_for(suite: Suite, runs: list[Run]) -> list[CheckResult]:
    """A check passes for a case only if it passes on every repeat."""
    per_run = [run_checks(suite.checks, r.output) for r in runs]
    out = []
    for i, check in enumerate(suite.checks):
        results = [pr[i] for pr in per_run]
        failed = next((r for r in results if not r.passed), None)
        out.append(failed or results[0])
    return out


def build_comparison(store: Store, suite_id: str, current_id: str, new_id: str, live: bool) -> Comparison:
    suite = store.load_suite(suite_id)
    cases = store.load_cases(suite_id)
    current = store.load_version(suite_id, current_id)
    new = store.load_version(suite_id, new_id)
    cur_res = load_results(store, suite_id, current_id, live)
    new_res = load_results(store, suite_id, new_id, live)
    comp = Comparison(suite=suite, current=current, new=new, cases=cases,
                      missing={current_id: [], new_id: []})
    crit_ids = [c.id for c in suite.rubric]
    cur_used, new_used = [], []
    for case in cases:
        cur_runs = usable_runs(cur_res, suite, case)
        new_runs = usable_runs(new_res, suite, case)
        if cur_runs is None:
            comp.missing[current_id].append(case)
        if new_runs is None and new_id != current_id:
            comp.missing[new_id].append(case)
        if cur_runs is None or new_runs is None:
            continue
        cur_used.append(cur_runs)
        new_used.append(new_runs)
        cur_checks = _checks_for(suite, cur_runs)
        new_checks = _checks_for(suite, new_runs)
        cur_score = scoring.case_score(cur_runs, crit_ids)
        new_score = scoring.case_score(new_runs, crit_ids)
        result = scoring.classify(
            cur_score, new_score, suite.threshold,
            [c.passed for c in cur_checks], [c.passed for c in new_checks],
        )
        criteria = [
            CriterionRow(
                criterion=crit,
                current=scoring.criterion_mean(cur_runs, crit.id),
                new=scoring.criterion_mean(new_runs, crit.id),
                current_reason=cur_runs[0].scores[crit.id].reason,
                new_reason=new_runs[0].scores[crit.id].reason,
            )
            for crit in suite.rubric
        ]
        comp.rows.append(CaseRow(
            case=case,
            current_score=cur_score,
            new_score=new_score,
            change=scoring.score_change(cur_score, new_score),
            result=result,
            current_output=cur_runs[0].output,
            new_output=new_runs[0].output,
            criteria=criteria,
            checks=[CheckRow(c.label, a, b) for c, a, b in zip(suite.checks, cur_checks, new_checks)],
        ))
    order = {scoring.WORSE: 0, scoring.BETTER: 1, scoring.SAME: 2}
    comp.rows.sort(key=lambda r: order[r.result])
    comp.current_info = _info(cur_res, cur_used)
    comp.new_info = _info(new_res, new_used)
    return comp


def prompt_diff(old: str, new: str) -> list[tuple[str, str]]:
    """Line-by-line diff as (kind, text) with kind Same, Removed or Added."""
    out = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        tag, text = line[:2], line[2:]
        if tag == "  ":
            out.append(("Same", text))
        elif tag == "- ":
            out.append(("Removed", text))
        elif tag == "+ ":
            out.append(("Added", text))
    return out


# shareable summary -----------------------------------------------------------
def summary_text(comp: Comparison, markdown: bool = False) -> str:
    f = scoring.fmt
    title = f"{comp.suite.name}: {comp.current.id} vs {comp.new.id}"
    lines = []
    if markdown:
        lines += [f"## Rollout Ready: {title}", "", f"**Verdict: {comp.verdict}**", ""]
    else:
        lines += [f"Rollout Ready: {title}", f"Verdict: {comp.verdict}"]
    lines.append(comp.explanation)
    lines.append("")
    bullet = "- " if markdown else ""
    lines.append(f"{bullet}Worse: {comp.worse}, Better: {comp.better}, No clear change: {comp.same}")
    lines.append(f"{bullet}Average score: {comp.current.id} {f(comp.avg_current)}, {comp.new.id} {f(comp.avg_new)}")
    if comp.worse_rows:
        lines += ["", "### Cases that got worse" if markdown else "Cases that got worse:"]
        for r in comp.worse_rows:
            label = f" ({r.case.label})" if r.case.label else ""
            lines.append(f"- {r.case.name}{label}: {f(r.current_score)} to {f(r.new_score)} ({scoring.fmt_change(r.change)})")
            drop = r.biggest_drop
            if drop:
                lines.append(f"  Biggest drop: {drop.criterion.name}, {scoring.fmt_criterion(drop.current)} to {scoring.fmt_criterion(drop.new)}.")
                lines.append(f"  Judge: {drop.new_reason}")
            for chk in r.broken_checks:
                lines.append(f"  Check now fails: {chk.label} ({chk.new.detail}).")
    return "\n".join(lines) + "\n"
