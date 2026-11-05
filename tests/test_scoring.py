import pytest

from rollout_ready import scoring
from rollout_ready.scoring import BETTER, SAME, WORSE, classify, verdict


def test_drop_of_exactly_threshold_is_worse():
    assert classify(4.0, 3.5, 0.5) == WORSE


def test_drop_just_under_threshold_is_no_clear_change():
    assert classify(4.0, 3.51, 0.5) == SAME
    assert classify(4.0, 3.6, 0.5) == SAME


def test_rise_of_exactly_threshold_is_better():
    assert classify(3.5, 4.0, 0.5) == BETTER


def test_rise_just_under_threshold_is_no_clear_change():
    assert classify(3.5, 3.99, 0.5) == SAME
    assert classify(3.6, 4.0, 0.5) == SAME


def test_floating_point_noise_does_not_flip_a_result():
    # 4.1 - 3.6 is 0.49999999999999956 in floating point; rounding keeps it at 0.5.
    assert 4.1 - 3.6 < 0.5
    assert classify(3.6, 4.1, 0.5) == BETTER
    assert classify(4.1, 3.6, 0.5) == WORSE
    assert scoring.score_change(4.25, 3.75) == -0.5


def test_check_that_now_fails_makes_a_case_worse_even_when_score_rises():
    assert classify(3.0, 4.4, 0.5, [True, True], [True, False]) == WORSE


def test_check_that_now_passes_makes_a_case_better_with_no_score_change():
    assert classify(3.0, 3.0, 0.5, [False, True], [True, True]) == BETTER


def test_unchanged_checks_do_not_affect_result():
    assert classify(3.0, 3.2, 0.5, [False, True], [False, True]) == SAME


def test_all_five_verdicts():
    assert verdict(0, 1) == "Ready to roll out"
    assert verdict(0, 0) == "No clear difference"
    assert verdict(2, 4) == "Needs review"
    assert verdict(2, 2) == "Not ready to roll out"
    assert verdict(2, 1) == "Not ready to roll out"
    assert verdict(1, 0) == "Not ready to roll out"
    assert verdict(0, 3, complete=False) == "Test incomplete"


def test_explanation_calls_out_a_hidden_problem():
    text = scoring.explanation(scoring.NOT_READY, "Version 3", "Version 2", 2, 4, 3.6, 3.875)
    assert text == ("Version 3 made 4 examples better and 2 examples worse. The average score went up "
                    "from 3.6 to 3.9, which would have hidden both problems.")


def test_explanation_for_ready_and_not_ready():
    assert scoring.explanation(scoring.READY, "Version 4", "Version 2", 0, 4, 3.6, 4.1, total=8) == (
        "Passed your checks on 8 saved examples: 4 got better, none got worse, and no deal-breakers were missed. "
        "Review the results before updating your team's tool.")
    assert scoring.explanation(scoring.NOT_READY, "Version 3", "Version 2", 2, 1, 3.875, 3.7) == (
        "Version 3 made 1 example better and 2 examples worse. The average score went down from 3.9 to 3.7.")


def test_explanation_when_only_a_deal_breaker_blocks():
    assert scoring.explanation(scoring.NOT_READY, "Version 2", "Version 1", 0, 2, 3.5, 4.0) == (
        "Version 2 made 2 examples better and none worse, but it still misses a deal-breaker. "
        "The average score went up from 3.5 to 4.0.")


def test_result_labels_use_plain_words():
    assert scoring.RESULT_LABELS == {WORSE: "Got worse", BETTER: "Got better", SAME: "About the same"}


# deal-breakers -----------------------------------------------------------------
def _runs(**scores):
    from rollout_ready.models import Run, Score
    return [Run(output="x", scores={k: Score(score=v, reason="r") for k, v in scores.items()})]


def _rubric():
    from rollout_ready.models import Criterion
    return [Criterion(id="facts", name="Facts right", five_looks_like="a", one_looks_like="b", deal_breaker=True),
            Criterion(id="tone", name="Tone", five_looks_like="a", one_looks_like="b")]


def test_deal_breaker_cutoff_two_misses_three_does_not():
    assert scoring.DEAL_BREAKER_CUTOFF == 2
    assert [c.id for c in scoring.missed_deal_breakers(_runs(facts=2, tone=5), _rubric())] == ["facts"]
    assert [c.id for c in scoring.missed_deal_breakers(_runs(facts=1, tone=5), _rubric())] == ["facts"]
    assert scoring.missed_deal_breakers(_runs(facts=3, tone=5), _rubric()) == []


def test_only_deal_breakers_can_be_missed():
    assert scoring.missed_deal_breakers(_runs(facts=4, tone=1), _rubric()) == []


def test_deal_breaker_missed_in_any_repetition_counts_even_when_the_mean_passes():
    from rollout_ready.models import Run, Score
    runs = [Run(output="a", scores={"facts": Score(score=5, reason=""), "tone": Score(score=4, reason="")}),
            Run(output="b", scores={"facts": Score(score=2, reason="Wrong date."), "tone": Score(score=4, reason="")})]
    assert [c.id for c in scoring.missed_deal_breakers(runs, _rubric())] == ["facts"]  # mean 3.5, but run 2 scored 2


def test_newly_missed_deal_breaker_counts_as_worse_even_with_no_score_change():
    assert classify(3.8, 3.8, 0.5, newly_missed=True) == WORSE
    assert classify(3.0, 4.4, 0.5, newly_missed=True) == WORSE
    assert classify(3.8, 3.8, 0.5, newly_missed=False) == SAME


def test_verdict_is_never_ready_with_a_missed_deal_breaker():
    assert verdict(0, 3, misses_deal_breaker=True) == "Not ready to roll out"
    assert verdict(0, 3, misses_deal_breaker=False) == "Ready to roll out"
    for worse in range(0, 4):
        for better in range(0, 6):
            assert verdict(worse, better, misses_deal_breaker=True) != "Ready to roll out"


def test_a_missed_deal_breaker_makes_every_complete_verdict_not_ready():
    assert verdict(0, 0, misses_deal_breaker=True) == "Not ready to roll out"
    assert verdict(2, 4, misses_deal_breaker=True) == "Not ready to roll out"
    assert verdict(2, 1, misses_deal_breaker=True) == "Not ready to roll out"


def test_case_score_averages_criteria_and_repeats():
    from rollout_ready.models import Run, Score
    runs = [Run(output="a", scores={"x": Score(score=4, reason=""), "y": Score(score=2, reason="")}),
            Run(output="b", scores={"x": Score(score=5, reason=""), "y": Score(score=5, reason="")})]
    assert scoring.case_score(runs, ["x", "y"]) == 4.0


def test_scores_display_to_one_decimal():
    assert scoring.fmt(3.875) == "3.9"
    assert scoring.fmt(4) == "4.0"
    assert scoring.fmt_change(-1.2) == "-1.2"


# five verdicts, in the spec's order (part of spec test 8) ----------------------------
@pytest.mark.parametrize("worse,better,miss,complete,expected", [
    (0, 0, False, False, "Test incomplete"),
    (3, 0, True, False, "Test incomplete"),       # incomplete wins over everything
    (0, 4, False, False, "Test incomplete"),
    (0, 4, True, True, "Not ready to roll out"),  # a deal-breaker miss beats a good count
    (1, 0, False, True, "Not ready to roll out"),
    (2, 2, False, True, "Not ready to roll out"),  # no more better than worse
    (1, 2, False, True, "Needs review"),
    (2, 3, False, True, "Needs review"),
    (0, 1, False, True, "Ready to roll out"),
    (0, 0, False, True, "No clear difference"),
])
def test_verdict_order(worse, better, miss, complete, expected):
    assert verdict(worse, better, misses_deal_breaker=miss, complete=complete) == expected


def test_keep_the_current_version_is_gone():
    assert not hasattr(scoring, "KEEP_CURRENT")
    assert "Keep the current version" not in scoring.VERDICT_KEYS


def test_test_incomplete_never_gives_a_verdict_sentence():
    text = scoring.explanation(scoring.INCOMPLETE, "Version 4", "Version 2", 0, 4, 3.6, 4.1, total=8)
    assert text == "Some answers or reviews are missing, so there is no verdict yet."


def test_needs_review_sentence_calls_out_a_hidden_problem():
    assert scoring.explanation(scoring.NEEDS_REVIEW, "Version 3", "Partner v2", 1, 3, 3.6, 3.9) == (
        "Version 3 made 3 examples better and 1 example worse. The average score went up from 3.6 to 3.9, "
        "which would have hidden the problem.")


def test_not_ready_sentence_when_only_a_deal_breaker_blocks_and_nothing_moved():
    assert scoring.explanation(scoring.NOT_READY, "Version 2", "Version 1", 0, 0, 3.8, 3.8) == (
        "Version 2 did about the same as version 1 on every example, but it misses a deal-breaker. "
        "The average score stayed at 3.8.")


@pytest.mark.parametrize("v,code", [("Ready to roll out", 0), ("No clear difference", 0),
                                    ("Not ready to roll out", 1), ("Needs review", 1), ("Test incomplete", 2)])
def test_exit_codes(v, code):
    assert scoring.exit_code(v) == code


# a deal-breaker miss names its repetition (part of spec test 5) ---------------------
def test_deal_breaker_misses_name_the_repetition_score_and_reason():
    from rollout_ready.models import Run, Score
    runs = [Run(output="a", scores={"facts": Score(score=4, reason="Right."), "tone": Score(score=1, reason="Stiff.")}),
            Run(output="b", scores={"facts": Score(score=1, reason="Made up a price."), "tone": Score(score=5, reason="")}),
            Run(output="c", scores={"facts": Score(score=5, reason="Right."), "tone": Score(score=5, reason="")})]
    misses = scoring.deal_breaker_misses(runs, _rubric())
    assert [(c.id, rep, score, reason) for c, rep, score, reason in misses] == [("facts", 2, 1, "Made up a price.")]
    assert scoring.case_score(runs, ["facts", "tone"]) > 3  # the average passes, the miss still counts
