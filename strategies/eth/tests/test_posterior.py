"""GBM posterior tests."""

from __future__ import annotations

import math
import unittest

from strategies.eth.model.posterior import (
    EmpiricalReturnPosterior,
    GBMPosterior,
    bracket_probability,
    horizon_log_returns,
    make_eth_posterior,
)


class GBMPosteriorTests(unittest.TestCase):

    def test_cdf_at_spot_is_half_with_zero_drift(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=1.0, mu_hourly=0.0)
        # log(2300/2300) = 0; CDF(0) = 0.5
        self.assertAlmostEqual(post.cdf(2300.0), 0.5, places=4)

    def test_high_strike_low_prob(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=1.0)
        # 50% above current is way out — should be near 0
        self.assertLess(post.prob_greater_than(3450.0), 0.001)

    def test_low_strike_high_prob(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=1.0)
        # 50% below current — should be near 1
        self.assertGreater(post.prob_greater_than(1150.0), 0.999)

    def test_drift_shifts_distribution(self):
        # With positive drift, prob of being above spot should rise
        no_drift = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=4.0, mu_hourly=0.0)
        with_drift = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=4.0, mu_hourly=0.005)
        self.assertGreater(with_drift.prob_greater_than(2300.0),
                           no_drift.prob_greater_than(2300.0))

    def test_cdf_monotonic(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.02, hours_to_settle=1.0)
        prev = -1.0
        for x in range(1500, 3500, 50):
            cur = post.cdf(float(x))
            self.assertGreaterEqual(cur, prev - 1e-9)
            prev = cur

    def test_horizon_scaling_widens_distribution(self):
        # Longer horizon → wider distribution → less mass on prob_greater_than(spot+small)
        short = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=1.0)
        long = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=24.0)
        # P(P_T > spot+10) should be roughly the same near 0.5, but the
        # tail probabilities differ sharply
        self.assertGreater(long.prob_greater_than(2400.0), short.prob_greater_than(2400.0))

    def test_zero_vol_collapses(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.0, hours_to_settle=1.0, mu_hourly=0.0)
        # No variance + no drift: degenerate point mass at spot
        self.assertEqual(post.prob_greater_than(2299.0), 1.0)
        self.assertEqual(post.prob_greater_than(2301.0), 0.0)

    def test_bracket_dispatch(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.015, hours_to_settle=1.0)
        # greater
        p = bracket_probability(post, floor_strike=2200.0, cap_strike=None, strike_type="greater")
        self.assertGreater(p, 0.5)
        # less
        p = bracket_probability(post, floor_strike=None, cap_strike=2400.0, strike_type="less")
        self.assertGreater(p, 0.5)
        # between
        p = bracket_probability(post, floor_strike=2250.0, cap_strike=2350.0, strike_type="between")
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1.0)
        # bad type
        with self.assertRaises(ValueError):
            bracket_probability(post, 100.0, 200.0, "wat")

    def test_negative_or_zero_x_zero_cdf(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.01, hours_to_settle=1.0)
        self.assertEqual(post.cdf(0.0), 0.0)
        self.assertEqual(post.cdf(-100.0), 0.0)

    def test_cdf_plus_sf_is_one(self):
        post = GBMPosterior(spot=2300.0, sigma_hourly=0.015, hours_to_settle=2.0)
        for x in (1500.0, 2300.0, 2500.0, 3000.0):
            self.assertAlmostEqual(post.cdf(x) + post.sf(x), 1.0, places=8)


class HorizonLogReturnsTests(unittest.TestCase):

    def test_one_hour_passthrough(self):
        rets = [0.01, -0.005, 0.002, -0.001]
        self.assertEqual(horizon_log_returns(rets, 1), rets)

    def test_two_hour_overlapping(self):
        rets = [0.01, -0.005, 0.002, -0.001]
        # h=2: [r0+r1, r1+r2, r2+r3]
        out = horizon_log_returns(rets, 2)
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(out[0], 0.005)
        self.assertAlmostEqual(out[1], -0.003)
        self.assertAlmostEqual(out[2], 0.001)

    def test_too_short(self):
        self.assertEqual(horizon_log_returns([0.01], 5), [])

    def test_full_horizon(self):
        rets = [0.01, 0.02, 0.03]
        # h=3 → 1 sample = sum of all
        out = horizon_log_returns(rets, 3)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0], 0.06)


class EmpiricalReturnPosteriorTests(unittest.TestCase):

    def test_cdf_at_spot_is_one_above_neg_returns(self):
        # All log returns are positive → P(P_T <= spot) = 0
        post = EmpiricalReturnPosterior(spot=2300.0, sorted_log_returns=(0.01, 0.02, 0.03))
        self.assertEqual(post.cdf(2300.0), 0.0)

    def test_cdf_at_spot_is_zero_below_pos_returns(self):
        # All log returns are negative → all realized prices < spot → P(<=spot) = 1
        post = EmpiricalReturnPosterior(spot=2300.0, sorted_log_returns=(-0.03, -0.02, -0.01))
        self.assertEqual(post.cdf(2300.0), 1.0)

    def test_cdf_step_function(self):
        # 4 equally-spaced log returns
        post = EmpiricalReturnPosterior(
            spot=100.0,
            sorted_log_returns=(math.log(0.95), math.log(0.98), math.log(1.02), math.log(1.05)),
        )
        # At x=99 (log return = log(0.99)), 2 samples are below → 2/4
        self.assertAlmostEqual(post.cdf(99.0), 0.5, places=6)
        # At x=110 (log return = log(1.10)), all 4 samples below → 1.0
        self.assertEqual(post.cdf(110.0), 1.0)
        # At x=90 (log return = log(0.90)), 0 samples below → 0.0
        self.assertEqual(post.cdf(90.0), 0.0)

    def test_fat_tails_vs_gaussian(self):
        # Construct a fat-tailed distribution: mostly small returns, occasional big
        sigma = 0.01
        # Samples: many at ±0.005 (within 1 σ), few at ±0.05 (5σ outliers)
        small = [0.005, -0.005, 0.005, -0.005] * 10  # 40 small
        big = [0.05, -0.05]  # 2 outliers at 5σ
        empirical = EmpiricalReturnPosterior(
            spot=100.0,
            sorted_log_returns=tuple(sorted(small + big)),
        )
        # Gaussian with same variance — but variance dominated by outliers
        # The empirical distribution puts more probability on extreme moves
        # P(P_T > 105) — log return > log(1.05) ≈ 0.0488 → 1 sample (the +0.05)
        empirical_p = empirical.prob_greater_than(105.0)
        self.assertGreater(empirical_p, 0.0)
        self.assertLess(empirical_p, 0.1)

    def test_uninformative_with_no_samples(self):
        post = EmpiricalReturnPosterior(spot=100.0, sorted_log_returns=())
        self.assertEqual(post.cdf(50.0), 0.5)
        self.assertEqual(post.cdf(200.0), 0.5)

    def test_bracket_dispatch_works_with_empirical(self):
        post = EmpiricalReturnPosterior(
            spot=2300.0,
            sorted_log_returns=tuple(sorted([-0.02, -0.01, 0.0, 0.01, 0.02] * 5)),
        )
        p_gt = bracket_probability(post, 2300.0, None, "greater")
        p_lt = bracket_probability(post, None, 2300.0, "less")
        self.assertAlmostEqual(p_gt + p_lt, 1.0, places=6)


class MakeEthPosteriorTests(unittest.TestCase):

    def test_picks_empirical_when_enough_samples(self):
        rets = [0.001 * i for i in range(-30, 31)]  # 61 samples
        post = make_eth_posterior(
            spot=2300.0,
            sigma_hourly=0.01,
            hourly_log_returns=rets,
            hours_to_settle=1.0,
            min_empirical_samples=24,
        )
        self.assertIsInstance(post, EmpiricalReturnPosterior)

    def test_falls_back_to_gbm_when_too_few(self):
        rets = [0.001, 0.002]
        post = make_eth_posterior(
            spot=2300.0,
            sigma_hourly=0.01,
            hourly_log_returns=rets,
            hours_to_settle=1.0,
            min_empirical_samples=24,
        )
        self.assertIsInstance(post, GBMPosterior)

    def test_raises_when_no_samples_no_sigma(self):
        with self.assertRaises(ValueError):
            make_eth_posterior(spot=2300.0, hours_to_settle=1.0)

    def test_horizon_scaling_changes_samples(self):
        # 100 hourly returns → 99 two-hour samples
        rets = [0.001] * 100
        post1 = make_eth_posterior(
            spot=2300.0,
            sigma_hourly=0.01,
            hourly_log_returns=rets,
            hours_to_settle=1.0,
        )
        post2 = make_eth_posterior(
            spot=2300.0,
            sigma_hourly=0.01,
            hourly_log_returns=rets,
            hours_to_settle=2.0,
        )
        assert isinstance(post1, EmpiricalReturnPosterior)
        assert isinstance(post2, EmpiricalReturnPosterior)
        self.assertEqual(len(post1.sorted_log_returns), 100)
        self.assertEqual(len(post2.sorted_log_returns), 99)


if __name__ == "__main__":
    unittest.main()
