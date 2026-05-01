"""Synthetic-data tests for the posterior model.

These don't hit the network. They verify the qualitative properties the
strategy depends on: late-day distributions collapse to the observation,
bracket probabilities sum to 1, the posterior responds correctly to
edge-case inputs.
"""

from __future__ import annotations

import math
import unittest

from strategies.weather.model.climo import ResidualParams, residual_at_hour
from strategies.weather.model.posterior import (
    EmpiricalPosterior,
    GaussianPosterior,
    Posterior,
    bracket_probability,
    daily_high_posterior,
)


class PosteriorTests(unittest.TestCase):

    def test_late_day_distribution_collapses(self):
        """At hour 23, posterior should be a tight band around obs_max."""
        post = daily_high_posterior(obs_max_so_far=72.0, hour_local=23)
        # P(daily_high <= 72) should be near 0.5 (point of collapse) since
        # mu_lift is ~0 and sigma_lift is small
        self.assertAlmostEqual(post.cdf(72.0), 0.5, delta=0.01)
        # P(daily_high > 75) should be ~0
        self.assertLess(post.sf(75.0), 0.01)
        # P(daily_high < 69) should be ~0 (cannot go BELOW obs_max)
        self.assertEqual(post.cdf(69.0), 0.0)

    def test_morning_distribution_wide(self):
        """At hour 6, posterior should expect significant additional warming."""
        post = daily_high_posterior(obs_max_so_far=55.0, hour_local=6)
        # Expected daily high should be well above obs_max
        # Median of obs_max + max(0, X) is approximately obs_max + mu_lift if mu_lift > 0
        residual = residual_at_hour(6)
        expected_high = 55.0 + residual.mu_lift_f
        # At median CDF should be ~0.5
        self.assertAlmostEqual(post.cdf(expected_high), 0.5, delta=0.05)
        # Wide uncertainty band
        self.assertGreater(post.sf(expected_high + 5), 0.1)

    def test_cdf_monotonic(self):
        post = daily_high_posterior(obs_max_so_far=70.0, hour_local=10)
        prev = -1.0
        for t in range(60, 100):
            cur = post.cdf(float(t))
            self.assertGreaterEqual(cur, prev - 1e-9)
            prev = cur

    def test_below_obs_max_is_impossible(self):
        post = daily_high_posterior(obs_max_so_far=80.0, hour_local=14)
        self.assertEqual(post.cdf(70.0), 0.0)
        self.assertEqual(post.cdf(79.5), 0.0)

    def test_bracket_probabilities_sum_to_one(self):
        """A complete bracket array should sum to 1.0 (within rounding).

        Mirrors Kalshi's actual KXHIGHNY bracket structure observed live:
        ``less<62``, ``62-63``, ``64-65``, ``66-67``, ``68-69``, ``greater>69``.
        Boundaries align so coverage is gap-free across all integers.
        """
        post = daily_high_posterior(obs_max_so_far=68.0, hour_local=10)
        bottom_cap_strike = 62
        middle = [(62, 63), (64, 65), (66, 67), (68, 69)]
        top_floor_strike = 69

        p_bottom = post.prob_less_than(bottom_cap_strike)
        p_middle = sum(post.prob_between(lo, hi) for lo, hi in middle)
        p_top = post.prob_greater_than(top_floor_strike)
        total = p_bottom + p_middle + p_top
        self.assertAlmostEqual(total, 1.0, delta=0.001)

    def test_bracket_probability_dispatch(self):
        post = daily_high_posterior(obs_max_so_far=70.0, hour_local=12)
        p_greater = bracket_probability(post, floor_strike=68, cap_strike=None, strike_type="greater")
        p_less = bracket_probability(post, floor_strike=None, cap_strike=68, strike_type="less")
        p_between = bracket_probability(post, floor_strike=66, cap_strike=67, strike_type="between")
        self.assertGreaterEqual(p_greater, 0.0)
        self.assertLessEqual(p_greater, 1.0)
        self.assertEqual(p_less, 0.0)  # high already at 70, can't be < 68
        self.assertGreaterEqual(p_between, 0.0)
        with self.assertRaises(ValueError):
            bracket_probability(post, None, None, "wat")

    def test_residual_table_complete(self):
        for h in range(24):
            params = residual_at_hour(h)
            self.assertIsInstance(params, ResidualParams)
            self.assertGreaterEqual(params.sigma_lift_f, 0.0)

    def test_residual_decreases_through_afternoon(self):
        """Sanity: σ at hour 6 should be larger than σ at hour 18."""
        morning = residual_at_hour(6)
        evening = residual_at_hour(18)
        self.assertGreater(morning.sigma_lift_f, evening.sigma_lift_f)
        self.assertGreater(morning.mu_lift_f, evening.mu_lift_f)


class EmpiricalPosteriorTests(unittest.TestCase):
    """v3 empirical-CDF posterior."""

    def test_below_obs_max_is_impossible(self):
        post = EmpiricalPosterior(obs_max=70.0, sorted_residuals=(0.0, 0.0, 1.0, 2.0, 5.0))
        self.assertEqual(post.cdf(65.0), 0.0)
        self.assertEqual(post.cdf(69.9), 0.0)

    def test_cdf_at_obs_max_picks_up_zero_mass(self):
        # Half the residuals are 0 → P(daily_high = obs_max) = 0.5
        post = EmpiricalPosterior(obs_max=70.0, sorted_residuals=(0.0, 0.0, 1.0, 2.0))
        # cdf(obs_max) = P(future_lift <= 0) = 2/4 = 0.5
        self.assertAlmostEqual(post.cdf(70.0), 0.5)

    def test_empirical_cdf_step_function(self):
        post = EmpiricalPosterior(obs_max=70.0, sorted_residuals=(1.0, 2.0, 3.0, 4.0))
        # At t=70, future_lift=0, none of the residuals are <= 0 → 0
        self.assertEqual(post.cdf(70.0), 0.0)
        # At t=71, future_lift<=1, one sample (1.0) qualifies → 1/4 = 0.25
        self.assertAlmostEqual(post.cdf(71.0), 0.25)
        # At t=73, future_lift<=3, three samples qualify → 3/4 = 0.75
        self.assertAlmostEqual(post.cdf(73.0), 0.75)
        # At t=75, all qualify → 1.0
        self.assertAlmostEqual(post.cdf(75.0), 1.0)

    def test_bracket_probabilities_sum_to_one_empirical(self):
        # 100 synthetic residuals: half are 0, half spread 1..5
        residuals = [0.0] * 50 + [1.0, 2.0, 3.0, 4.0, 5.0] * 10
        post = EmpiricalPosterior(obs_max=68.0, sorted_residuals=tuple(sorted(residuals)))
        bottom_cap_strike = 62
        middle = [(62, 63), (64, 65), (66, 67), (68, 69), (70, 71), (72, 73)]
        top_floor_strike = 73
        p_bottom = post.prob_less_than(bottom_cap_strike)
        p_middle = sum(post.prob_between(lo, hi) for lo, hi in middle)
        p_top = post.prob_greater_than(top_floor_strike)
        self.assertAlmostEqual(p_bottom + p_middle + p_top, 1.0, delta=0.001)

    def test_daily_high_posterior_picks_empirical_when_samples_given(self):
        post = daily_high_posterior(
            obs_max_so_far=70.0,
            hour_local=12,
            samples=[0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],
        )
        self.assertIsInstance(post, EmpiricalPosterior)

    def test_daily_high_posterior_falls_back_to_gaussian(self):
        # No samples → should be Gaussian
        post = daily_high_posterior(obs_max_so_far=70.0, hour_local=12)
        self.assertIsInstance(post, GaussianPosterior)

    def test_empty_samples_falls_back_to_gaussian(self):
        post = daily_high_posterior(obs_max_so_far=70.0, hour_local=12, samples=[])
        self.assertIsInstance(post, GaussianPosterior)

    def test_bracket_probability_dispatch_works_with_empirical(self):
        post = daily_high_posterior(
            obs_max_so_far=70.0,
            hour_local=15,
            samples=[0.0] * 80 + [1.0, 2.0, 3.0, 4.0, 5.0] * 4,
        )
        p = bracket_probability(post, floor_strike=68, cap_strike=None, strike_type="greater")
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)
        # high already at 70, so > 68 should be ~1
        self.assertAlmostEqual(p, 1.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
