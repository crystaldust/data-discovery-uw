import pytest

from ddm.eval.stats import Estimate, Stratum, ratio_estimate, stratified_proportion, wilson


def test_wilson_of_a_perfect_sample_has_a_lower_bound_below_one():
    low, high = wilson(100, 100)

    assert low == pytest.approx(0.963, abs=0.001)
    assert high == 1.0


def test_wilson_of_half_matches_the_textbook_interval():
    low, high = wilson(50, 100)

    assert (low, high) == (pytest.approx(0.404, abs=0.001), pytest.approx(0.596, abs=0.001))


def test_wilson_of_nothing_says_nothing():
    assert wilson(0, 0) == (0.0, 1.0)


def test_stratified_proportion_weights_each_stratum_by_its_population():
    strata = [Stratum(population=900, sampled=10, positives=10), Stratum(100, 10, 5)]

    estimate = stratified_proportion(strata)

    assert estimate.point == pytest.approx(0.9 * 1.0 + 0.1 * 0.5)
    assert estimate.low < estimate.point <= estimate.high <= 1.0


def test_a_fully_sampled_stratum_adds_no_sampling_error():
    estimate = stratified_proportion([Stratum(population=10, sampled=10, positives=7)])

    assert estimate == Estimate(0.7, 0.7, 0.7)


def test_an_unlabelled_stratum_with_members_makes_the_estimate_unknown():
    assert stratified_proportion([Stratum(100, 0, 0), Stratum(100, 4, 4)]) is None


def test_empty_strata_are_ignored():
    estimate = stratified_proportion([Stratum(0, 0, 0), Stratum(100, 4, 4)])

    assert estimate.point == 1.0


def test_an_all_positive_sample_still_has_a_real_interval():
    estimate = stratified_proportion([Stratum(1000, 40, 40), Stratum(5000, 50, 50)])

    assert estimate.point == 1.0
    assert estimate.low < 0.97


def test_stratified_proportion_of_no_labels_is_unknown():
    assert stratified_proportion([Stratum(100, 0, 0)]) is None


def test_recall_is_the_share_of_estimated_true_pairs_found_above_the_threshold():
    above = [Stratum(population=100, sampled=10, positives=8)]  # about 80 true pairs
    below = [Stratum(population=1000, sampled=20, positives=1)]  # about 50 true pairs

    estimate = ratio_estimate(above, below)

    assert estimate.point == pytest.approx(80 / 130)
    assert 0.0 <= estimate.low < estimate.point < estimate.high <= 1.0


def test_recall_without_any_true_pair_is_unknown():
    assert ratio_estimate([Stratum(10, 5, 0)], [Stratum(10, 5, 0)]) is None


def test_recall_is_unknown_while_a_populated_band_is_unlabelled():
    assert ratio_estimate([Stratum(100, 10, 8)], [Stratum(1000, 0, 0)]) is None


def test_an_all_negative_band_below_still_widens_the_recall_interval():
    estimate = ratio_estimate([Stratum(100, 10, 10)], [Stratum(5000, 20, 0)])

    assert estimate.point == 1.0
    assert estimate.low < 0.9


def test_recall_bounds_combine_each_bands_wilson_bounds():
    above = [Stratum(population=100, sampled=10, positives=8)]
    below = [Stratum(population=1000, sampled=20, positives=1)]
    a_low, a_high = (100 * b for b in wilson(8, 10))
    b_low, b_high = (1000 * b for b in wilson(1, 20))

    estimate = ratio_estimate(above, below)

    assert estimate.low == pytest.approx(a_low / (a_low + b_high))
    assert estimate.high == pytest.approx(a_high / (a_high + b_low))


def test_a_large_band_with_no_duplicates_in_its_sample_still_limits_recall():
    estimate = ratio_estimate([Stratum(500, 14, 10)], [Stratum(11000, 15, 0)])

    assert estimate.point == 1.0
    assert estimate.low < 0.2  # 0 of 15 cannot rule out hundreds of hidden duplicates
