"""Realized-vol estimator tests."""

from __future__ import annotations

import math
import unittest

from strategies.eth.model.volatility import (
    realized_vol,
    trailing_realized_vol,
)


class RealizedVolTests(unittest.TestCase):

    def test_known_std(self):
        # Returns with known std-dev: [-1, 1, -1, 1] has sample std = 1.1547
        rets = [-1.0, 1.0, -1.0, 1.0]
        v = realized_vol(rets)
        self.assertEqual(v.n_returns, 4)
        # sample std with n-1: var = sum(2^2)/3 = 4/3 → sd = sqrt(4/3) ≈ 1.1547
        self.assertAlmostEqual(v.sigma_hourly, math.sqrt(4 / 3), places=4)

    def test_zero_when_one_sample(self):
        v = realized_vol([0.005])
        self.assertEqual(v.sigma_hourly, 0.0)
        self.assertEqual(v.n_returns, 1)

    def test_zero_when_empty(self):
        v = realized_vol([])
        self.assertEqual(v.sigma_hourly, 0.0)
        self.assertEqual(v.n_returns, 0)

    def test_horizon_scaling(self):
        v = realized_vol([0.01, -0.01, 0.005, -0.005, 0.0, 0.0])
        # 4-hour horizon volatility = sigma * sqrt(4) = 2*sigma
        self.assertAlmostEqual(v.sigma_for_horizon(4.0), 2.0 * v.sigma_hourly)
        # 0.25-hour = 0.5*sigma
        self.assertAlmostEqual(v.sigma_for_horizon(0.25), 0.5 * v.sigma_hourly)

    def test_trailing_window(self):
        rets = [10.0, 10.0, 10.0, 10.0, 0.001, 0.001, 0.001]
        # Window covering only the small returns at the end → tiny sigma
        v = trailing_realized_vol(rets, window=3)
        self.assertEqual(v.n_returns, 3)
        self.assertLess(v.sigma_hourly, 0.01)
        # Full series → big sigma due to the 10s
        v_all = trailing_realized_vol(rets, window=0)
        self.assertGreater(v_all.sigma_hourly, 1.0)


if __name__ == "__main__":
    unittest.main()
