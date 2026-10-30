"""Scoring and verdict rules. Kept small and pure so they are easy to test.

- Criterion score: integer 1 to 5.
- Case score: mean of its criterion scores, averaged across repeats.
- Change: new minus current, rounded to 2 decimal places before comparing.
- Worse: change <= -threshold, or a check that passed now fails.
- Better: change >= threshold, or a check that failed now passes.
- Otherwise: no clear change.
"""

from __future__ import annotations

from statistics import mean

from .models import Run

WORSE = "worse"
BETTER = "better"
SAME = "same"

RESULT_LABELS = {WORSE: "Worse", BETTER: "Better", SAME: "No clear change"}

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


def classify(
    current: float,
    new: float,
    threshold: float,
    current_checks: list[bool] = (),
    new_checks: list[bool] = (),
) -> str:
    change = score_change(current, new)
    pairs = list(zip(current_checks, new_checks))
    check_broke = any(c and not n for c, n in pairs)
    check_fixed = any(n and not c for c, n in pairs)
    if change <= -threshold or check_broke:
        return WORSE
    if change >= threshold or check_fixed:
        return BETTER
    return SAME


def verdict(worse: int, better: int) -> str:
    if worse == 0:
        return READY if better >= 1 else NO_DIFFERENCE
    return NOT_READY if better > worse else KEEP_CURRENT


def blocks_rollout(verdict_text: str) -> bool:
    return verdict_text in (NOT_READY, KEEP_CURRENT)


def _cases(n: int) -> str:
    return "case" if n == 1 else "cases"


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
            f"{new_title} did about the same as {current_title.lower()} on every test case, "
            f"so nothing got clearly better or worse. The average score {avg}."
        )
    if better:
        counts = f"{new_title} made {better} {_cases(better)} better and {worse} {_cases(worse)} worse."
    else:
        counts = f"{new_title} made {worse} {_cases(worse)} worse and none better."
    if float(fmt(avg_new)) > float(fmt(avg_current)):
        return f"{counts} The average score {avg}, which would have hidden {_problems(worse)}."
    return f"{counts} The average score {avg}."
