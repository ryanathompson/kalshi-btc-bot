"""Tests for empirical climatology loading and fitter math."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from strategies.weather.data.iem import HourlyObservation
from strategies.weather.model.climo import (
    DEFAULT_RESIDUAL_TABLE,
    EmpiricalClimatology,
    MIN_SAMPLES_PER_CELL,
    ResidualParams,
    residual_at_hour,
)
from strategies.weather.model.fit_climo import (
    collect_residuals,
    fit_gaussian,
)


class FitGaussianTests(unittest.TestCase):

    def test_basic_mean_std(self):
        params = fit_gaussian([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(params.mu_lift_f, 3.0, places=4)
        # sample std with n-1 denominator: sqrt(2.5) ≈ 1.5811
        self.assertAlmostEqual(params.sigma_lift_f, 1.5811, places=3)

    def test_single_sample_zero_std(self):
        params = fit_gaussian([42.0])
        self.assertEqual(params.mu_lift_f, 42.0)
        self.assertEqual(params.sigma_lift_f, 0.0)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            fit_gaussian([])


class CollectResidualsTests(unittest.TestCase):

    def _make_obs(self, year, month, day, hour, tmpf):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        valid_local = datetime(year, month, day, hour, 0, tzinfo=tz)
        return HourlyObservation(
            station="NYC",
            valid_utc=valid_local.astimezone(timezone.utc),
            valid_local=valid_local,
            tmpf=tmpf,
        )

    def test_residuals_non_negative(self):
        # Build one day with rising temps then falling
        obs = [self._make_obs(2025, 6, 15, h, t)
               for h, t in [(6, 65), (10, 72), (14, 80), (18, 78), (22, 70)]]
        residuals = collect_residuals(obs)
        for samples in residuals.values():
            for r in samples:
                self.assertGreaterEqual(r, 0.0)

    def test_late_hour_residuals_zero(self):
        # Day's high observed by 14:00; later hours should have residual ~ 0
        obs = [self._make_obs(2025, 6, 15, h, t)
               for h, t in [(6, 65), (10, 72), (14, 80), (18, 78), (22, 70)]]
        residuals = collect_residuals(obs)
        # By hour 18, max-so-far = 80 = daily high → residual = 0
        # June = month 6
        self.assertIn((6, 18), residuals)
        self.assertEqual(residuals[(6, 18)][0], 0.0)
        self.assertEqual(residuals[(6, 22)][0], 0.0)

    def test_morning_residual_positive(self):
        obs = [self._make_obs(2025, 6, 15, h, t)
               for h, t in [(6, 65), (10, 72), (14, 80), (18, 78), (22, 70)]]
        residuals = collect_residuals(obs)
        # By hour 6, max-so-far = 65, daily high = 80, residual = 15
        self.assertEqual(residuals[(6, 6)][0], 15.0)
        # By hour 10, max-so-far = 72, residual = 8
        self.assertEqual(residuals[(6, 10)][0], 8.0)


class EmpiricalClimatologyTests(unittest.TestCase):

    def test_load_and_lookup(self):
        payload = {
            "city_code": "NYC",
            "fit_window": "2024-01-01..2024-12-31",
            "n_days": 360,
            "by_month_hour": {
                "1": {"6": {"mu_lift_f": 12.5, "sigma_lift_f": 5.0, "n": 25}},
                "7": {"14": {"mu_lift_f": 1.2, "sigma_lift_f": 1.5, "n": 28}},
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmp_path = Path(f.name)
        try:
            climo = EmpiricalClimatology.from_json_file(tmp_path)
        finally:
            tmp_path.unlink()

        # Filled cell: should return empirical
        params = climo.residual_at(month=1, hour_local=6)
        self.assertAlmostEqual(params.mu_lift_f, 12.5)
        self.assertAlmostEqual(params.sigma_lift_f, 5.0)

        params = climo.residual_at(month=7, hour_local=14)
        self.assertAlmostEqual(params.mu_lift_f, 1.2)
        self.assertAlmostEqual(params.sigma_lift_f, 1.5)

    def test_fallback_for_missing_cell(self):
        payload = {
            "city_code": "NYC", "fit_window": "x", "n_days": 0,
            "by_month_hour": {},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmp_path = Path(f.name)
        try:
            climo = EmpiricalClimatology.from_json_file(tmp_path)
        finally:
            tmp_path.unlink()

        # No empirical data → falls back to parametric
        params = climo.residual_at(month=3, hour_local=10)
        expected = residual_at_hour(10)
        self.assertAlmostEqual(params.mu_lift_f, expected.mu_lift_f)
        self.assertAlmostEqual(params.sigma_lift_f, expected.sigma_lift_f)

    def test_fallback_for_low_sample_cell(self):
        payload = {
            "city_code": "NYC", "fit_window": "x", "n_days": 0,
            "by_month_hour": {
                "3": {"10": {"mu_lift_f": 99.0, "sigma_lift_f": 99.0, "n": MIN_SAMPLES_PER_CELL - 1}}
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmp_path = Path(f.name)
        try:
            climo = EmpiricalClimatology.from_json_file(tmp_path)
        finally:
            tmp_path.unlink()

        # Low n → falls back, NOT to the suspicious empirical values
        params = climo.residual_at(month=3, hour_local=10)
        self.assertNotEqual(params.mu_lift_f, 99.0)

    def test_for_city_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = EmpiricalClimatology.for_city("XYZ", base_dir=Path(tmp))
            self.assertIsNone(result)

    def test_v3_samples_loaded(self):
        payload = {
            "city_code": "NYC", "fit_window": "x", "n_days": 0,
            "model_version": 3,
            "by_month_hour": {
                "1": {"6": {
                    "mu_lift_f": 5.0, "sigma_lift_f": 3.0,
                    "n": 50,
                    "residuals": [0.0, 0.0, 1.5, 2.0, 3.0, 4.5, 5.0, 6.0, 7.0, 8.0,
                                  0.0, 0.5, 1.0, 1.0, 2.5, 3.0, 4.0, 5.5, 6.5, 7.5,
                                  0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 4.5, 5.0,
                                  0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 4.5, 5.0,
                                  0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 4.5, 5.0],
                }},
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmp_path = Path(f.name)
        try:
            climo = EmpiricalClimatology.from_json_file(tmp_path)
        finally:
            tmp_path.unlink()
        self.assertEqual(climo.model_version, 3)
        samples = climo.samples_at(month=1, hour_local=6)
        self.assertIsNotNone(samples)
        self.assertEqual(len(samples), 50)
        # Sorted ascending
        self.assertEqual(samples, tuple(sorted(samples)))
        # Empty cell → None
        self.assertIsNone(climo.samples_at(month=2, hour_local=6))

    def test_v2_file_has_no_samples(self):
        payload = {
            "city_code": "NYC", "fit_window": "x", "n_days": 0,
            # model_version absent → v2
            "by_month_hour": {
                "1": {"6": {"mu_lift_f": 5.0, "sigma_lift_f": 3.0, "n": 50}},
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmp_path = Path(f.name)
        try:
            climo = EmpiricalClimatology.from_json_file(tmp_path)
        finally:
            tmp_path.unlink()
        self.assertEqual(climo.model_version, 2)
        # Gaussian works
        params = climo.residual_at(month=1, hour_local=6)
        self.assertAlmostEqual(params.mu_lift_f, 5.0)
        # No samples available
        self.assertIsNone(climo.samples_at(month=1, hour_local=6))

    def test_coverage_reports_filled_cells(self):
        payload = {
            "city_code": "NYC", "fit_window": "x", "n_days": 0,
            "by_month_hour": {
                "1": {"6": {"mu_lift_f": 1.0, "sigma_lift_f": 1.0, "n": 50}},
                "7": {"14": {"mu_lift_f": 1.0, "sigma_lift_f": 1.0, "n": 50}},
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            tmp_path = Path(f.name)
        try:
            climo = EmpiricalClimatology.from_json_file(tmp_path)
        finally:
            tmp_path.unlink()
        filled, total = climo.coverage()
        self.assertEqual(filled, 2)
        self.assertEqual(total, 12 * 24)


if __name__ == "__main__":
    unittest.main()
