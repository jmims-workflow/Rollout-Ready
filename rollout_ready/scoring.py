"""Scoring and verdict rules. Kept small and pure so they are easy to test.

- Criterion score: integer 1 to 5.
- Case score: mean of its criterion scores, averaged across repeats.
- Change: new minus current, rounded to 2 decimal places before comparing.
- Worse: change <= -threshold, a check that passed now fails, or the new
  version newly misses a deal-breaker (scores it DEAL_BREAKER_CUTOFF or lower).
- Better: change >= threshold, or a check that failed now passes.
- Otherwise: no clear change.
- The verdict is never "Ready to roll out" while the new version misses a
  deal-breaker on any example.

User-facing words: a case is an "example", Worse / Better / No clear change
are "Got worse" / "Got better" / "About the same".
"""

from __future__ import annotations

from statistics import mean

from .models import Criterion, Run

WORSE = "worse"
BETTER = "better"
SAME = "same"

RESULT_LABELS = {WORSE: "Got worse", BETTER: "Got better", SAME: "About the same"}

DEAL_BREAKER_CUTOFF = 2  # a deal-breaker scored this or lower is missed

READY = "Ready to roll out"
NO_DIFFERENCE = "No clear difference"
NOT_READY = "Not ready to roll out"
KEEP_CURRENT = "Keep the current version"

VERDICT_KEYS = {READY: "ready", NO_DIFFERENCE: "same", NOT_READY: "not-ready", KEEP_CURRENT: "keep"}


def fmt(score: float | None) -> str:
    """Scores are shown to one decimal place."""
    if score is None:
        return "-"
    return f"{score:.1f}"


def fmt_criterion(score: float) -> str:
    """Criterion scores are whole numbers unless averaged over repeats."""
    return str(int(score)) if float(score).is_integer() else f"{score:.1f}"


def fmt_change(change: float) -> str:
    if abs(change) < 0.05:
        return "0.0"
    return f"{change:+.1f}"


def run_score(run: Run, criterion_ids: list[str]) -> float:
    return mean(run.scores[c].score for c in criterion_ids)


def case_score(runs: list[Run], criterion_ids: list[str]) -> float:
    return mean(run_score(r, criterion_ids) for r in runs)


def criterion_mean(runs: list[Run], criterion_id: str) -> float:
    return mean(r.scores[criterion_id].score for r in runs)


def score_change(current: float, new: float) -> float:
    return round(new - current, 2)


def is_missed(score: float) -> bool:
    return score <= DEAL_BREAKER_CUTOFF


def missed_deal_breakers(runs: list[Run], rubric: list[Criterion]) -> list[Criterion]:
    """Deal-breakers this output misses, using the mean score across repeats."""
    return [c for c in rubric if c.deal_breaker and is_missed(criterion_mean(runs, c.id))]


def classify(
    current: float,
    new: float,
    threshold: float,
    current_checks: list[bool] = (),
    new_checks: list[bool] = (),
    newly_missed: bool = False,
) -> str:
    change = score_change(current, new)
    pairs = list(zip(current_checks, new_checks))
    check_broke = any(c and not n for c, n in pairs)
    check_fixed = any(n and not c for c, n in pairs)
    if change <= -threshold or check_broke or newly_missed:
        return WORSE
    if change >= threshold or check_fixed:
        return BETTER
    return SAME


def verdict(worse: int, better: int, misses_deal_breaker: bool = False) -> str:
    if worse == 0:
        if better >= 1:
            return NOT_READY if misses_deal_breaker else READY
        return NO_DIFFERENCE
    return NOT_READY if better > worse else KEEP_CURRENT


def blocks_rollout(verdict_text: str) -> bool:
    return verdict_text in (NOT_READY, KEEP_CURRENT)


def _cases(n: int) -> str:
    return "example" if n == 1 else "examples"


def _problems(n: int) -> str:
    if n == 1:
        return "the problem"
    if n == 2:
        return "both problems"
    return f"all {n} problems"


def _average_phrase(avg_current: float, avg_new: float) -> str:
    a, b = fmt(avg_current), fmt(avg_new)
    if float(b) > float(a):
        return f"went up from {a} to {b}"
    if float(b) < float(a):
        return f"went down from {a} to {b}"
    return f"stayed at {a}"


def explanation(
    verdict_text: str,
    new_title: str,
    current_title: str,
    worse: int,
    better: int,
    avg_current: float,
    avg_new: float,
) -> str:
    avg = _average_phrase(avg_current, avg_new)
    if verdict_text == READY:
        return f"{new_title} made {better} {_cases(better)} better and none worse. The average score {avg}."
    if verdict_text == NO_DIFFERENCE:
        return (
            f"{new_title} did about the same as {current_title.lower()} on every example, "
            f"so nothing got clearly better or worse. The average score {avg}."
        )
    if verdict_text == NOT_READY and not worse:
        return (
            f"{new_title} made {better} {_cases(better)} better and none worse, "
            f"but it still misses a deal-breaker. The average score {avg}."
        )
    if better:
        counts = f"{new_title} made {better} {_cases(better)} better and {worse} {_cases(worse)} worse."
    else:
        counts = f"{new_title} made {worse} {_cases(worse)} worse and none better."
    if float(fmt(avg_new)) > float(fmt(avg_current)):
        return f"{counts} The average score {avg}, which would have hidden {_problems(worse)}."
    return f"{counts} The average score {avg}."
