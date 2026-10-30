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


def test_all_four_verdicts():
    assert verdict(0, 1) == "Ready to roll out"
    assert verdict(0, 0) == "No clear difference"
    assert verdict(2, 4) == "Not ready to roll out"
    assert verdict(2, 2) == "Keep the current version"
    assert verdict(2, 1) == "Keep the current version"
    assert verdict(1, 0) == "Keep the current version"


def test_explanation_calls_out_a_hidden_problem():
    text = scoring.explanation(scoring.NOT_READY, "Version 3", "Version 2", 2, 4, 3.6, 3.875)
    assert text == ("Version 3 made 4 cases better and 2 cases worse. The average score went up "
                    "from 3.6 to 3.9, which would have hidden both problems.")


def test_explanation_for_ready_and_keep():
    assert scoring.explanation(scoring.READY, "Version 4", "Version 2", 0, 4, 3.6, 4.1) == (
        "Version 4 made 4 cases better and none worse. The average score went up from 3.6 to 4.1.")
    assert scoring.explanation(scoring.KEEP_CURRENT, "Version 3", "Version 2", 2, 1, 3.875, 3.7) == (
        "Version 3 made 1 case better and 2 cases worse. The average score went down from 3.9 to 3.7.")


def test_case_score_averages_criteria_and_repeats():
    from rollout_ready.models import Run, Score
    runs = [Run(output="a", scores={"x": Score(score=4, reason=""), "y": Score(score=2, reason="")}),
            Run(output="b", scores={"x": Score(score=5, reason=""), "y": Score(score=5, reason="")})]
    assert scoring.case_score(runs, ["x", "y"]) == 4.0


def test_scores_display_to_one_decimal():
    assert scoring.fmt(3.875) == "3.9"
    assert scoring.fmt(4) == "4.0"
    assert scoring.fmt_change(-1.2) == "-1.2"
