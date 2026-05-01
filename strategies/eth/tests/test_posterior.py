"""GBM posterior tests."""

from __future__ import annotations

import math
import unittest

from strategies.eth.model.posterior import GBMPosterior, bracket_probability


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


if __name__ == "__main__":
    unittest.main()
