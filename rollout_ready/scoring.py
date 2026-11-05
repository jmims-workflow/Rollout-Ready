"""Scoring and verdict rules. Kept small and pure so they are easy to test.

- Criterion score: integer 1 to 5.
- Case score: mean of its criterion scores, averaged across repeats.
- Change: new minus current, rounded to 2 decimal places before comparing.
- Worse: change <= -threshold, a check that passed now fails, or the new
  version newly misses a deal-breaker.
- Better: change >= threshold, or a check that failed now passes.
- Otherwise: no clear change.
- A deal-breaker is missed when the reviewer scores it DEAL_BREAKER_CUTOFF or
  lower in any repetition, even if the average across repetitions is higher.
- Verdicts, in this order (spec section 5):
  1. Test incomplete: any answer or review is missing, cut off or failed. No verdict.
  2. Not ready: the new version misses a deal-breaker on any example in any
     repetition, or at least one example got worse and no more got better.
  3. Needs review: some got worse, more got better, no deal-breaker missed.
  4. Ready to roll out: none worse, at least one better, no deal-breaker missed.
  5. No clear difference: none better or worse, no deal-breaker missed.
- Command line exit codes: 0 for Ready and No clear difference, 1 for Not
  ready and Needs review, 2 for Test incomplete.

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
NEEDS_REVIEW = "Needs review"
INCOMPLETE = "Test incomplete"

VERDICT_KEYS = {READY: "ready", NO_DIFFERENCE: "same", NOT_READY: "not-ready", NEEDS_REVIEW: "review",
                INCOMPLETE: "incomplete"}
EXIT_CODES = {READY: 0, NO_DIFFERENCE: 0, NOT_READY: 1, NEEDS_REVIEW: 1, INCOMPLETE: 2}


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


def deal_breaker_misses(runs: list[Run], rubric: list[Criterion]) -> list[tuple[Criterion, int, int, str]]:
    """Every deal-breaker miss: (quality, repetition number from 1, original score, reviewer's reason)."""
    return [(c, i, run.scores[c.id].score, run.scores[c.id].reason)
            for c in rubric if c.deal_breaker
            for i, run in enumerate(runs, start=1) if is_missed(run.scores[c.id].score)]


def missed_deal_breakers(runs: list[Run], rubric: list[Criterion]) -> list[Criterion]:
    """Deal-breakers this output misses in any repetition, even if the average is higher."""
    missed = {c.id for c, *_ in deal_breaker_misses(runs, rubric)}
    return [c for c in rubric if c.id in missed]


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


def verdict(worse: int, better: int, misses_deal_breaker: bool = False, complete: bool = True) -> str:
    """The five verdicts, applied in the order of spec section 5."""
    if not complete:
        return INCOMPLETE
    if misses_deal_breaker or (worse >= 1 and better <= worse):
        return NOT_READY
    if worse >= 1:
        return NEEDS_REVIEW
    if better >= 1:
        return READY
    return NO_DIFFERENCE


def exit_code(verdict_text: str) -> int:
    """For the command line: 0 Ready or No clear difference, 1 Not ready or Needs review, 2 Test incomplete."""
    return EXIT_CODES[verdict_text]


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
    total: int | None = None,
) -> str:
    if verdict_text == INCOMPLETE:
        return "Some answers or reviews are missing, so there is no verdict yet."
    avg = _average_phrase(avg_current, avg_new)
    if verdict_text == READY:
        n = better if total is None else total
        return (f"Passed your checks on {n} saved {_cases(n)}: {better} got better, none got worse, "
                "and no deal-breakers were missed. Review the results before updating your team's tool.")
    if verdict_text == NO_DIFFERENCE:
        return (
            f"{new_title} did about the same as {current_title.lower()} on every example, "
            f"so nothing got clearly better or worse. The average score {avg}."
        )
    if verdict_text == NOT_READY and not worse:
        if not better:
            return (f"{new_title} did about the same as {current_title.lower()} on every example, "
                    f"but it misses a deal-breaker. The average score {avg}.")
        return (
            f"{new_title} made {better} {_cases(better)} better and none worse, "
            f"but it still misses a deal-breaker. The average score {avg}."
        )
    # Not ready with examples that got worse, and Needs review: say when the average hides problems.
    if better:
        counts = f"{new_title} made {better} {_cases(better)} better and {worse} {_cases(worse)} worse."
    else:
        counts = f"{new_title} made {worse} {_cases(worse)} worse and none better."
    if float(fmt(avg_new)) > float(fmt(avg_current)):
        return f"{counts} The average score {avg}, which would have hidden {_problems(worse)}."
    return f"{counts} The average score {avg}."
