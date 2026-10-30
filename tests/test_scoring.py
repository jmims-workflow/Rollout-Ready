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
    assert text == ("Version 3 made 4 examples better and 2 examples worse. The average score went up "
                    "from 3.6 to 3.9, which would have hidden both problems.")


def test_explanation_for_ready_and_keep():
    assert scoring.explanation(scoring.READY, "Version 4", "Version 2", 0, 4, 3.6, 4.1) == (
        "Version 4 made 4 examples better and none worse. The average score went up from 3.6 to 4.1.")
    assert scoring.explanation(scoring.KEEP_CURRENT, "Version 3", "Version 2", 2, 1, 3.875, 3.7) == (
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


def test_deal_breaker_uses_the_mean_across_repeats():
    from rollout_ready.models import Run, Score
    runs = [Run(output="a", scores={"facts": Score(score=2, reason=""), "tone": Score(score=4, reason="")}),
            Run(output="b", scores={"facts": Score(score=3, reason=""), "tone": Score(score=4, reason="")})]
    assert scoring.missed_deal_breakers(runs, _rubric()) == []  # mean 2.5


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


def test_other_verdicts_follow_the_usual_rules_with_a_missed_deal_breaker():
    assert verdict(0, 0, misses_deal_breaker=True) == "No clear difference"
    assert verdict(2, 4, misses_deal_breaker=True) == "Not ready to roll out"
    assert verdict(2, 1, misses_deal_breaker=True) == "Keep the current version"


def test_case_score_averages_criteria_and_repeats():
    from rollout_ready.models import Run, Score
    runs = [Run(output="a", scores={"x": Score(score=4, reason=""), "y": Score(score=2, reason="")}),
            Run(output="b", scores={"x": Score(score=5, reason=""), "y": Score(score=5, reason="")})]
    assert scoring.case_score(runs, ["x", "y"]) == 4.0


def test_scores_display_to_one_decimal():
    assert scoring.fmt(3.875) == "3.9"
    assert scoring.fmt(4) == "4.0"
    assert scoring.fmt_change(-1.2) == "-1.2"
