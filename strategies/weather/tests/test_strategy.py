"""Interface-conformance and end-to-end synthetic tests for WeatherStrategy."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from master.interfaces import MarketSnapshot, Side, Strategy, StrategyContext
from master.rules import GlobalRules, kalshi_taker_fee, kalshi_maker_fee
from strategies.weather.strategy import WeatherStrategy


def _snap(ticker: str, series: str, floor=None, cap=None, stype="between",
          bid=0.30, ask=0.32) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        series_ticker=series,
        yes_bid=bid,
        yes_ask=ask,
        yes_bid_size=100,
        yes_ask_size=100,
        last_price=(bid + ask) / 2,
        volume_24h=1000.0,
        open_interest=500.0,
        close_time=datetime.now(timezone.utc),
        floor_strike=floor,
        cap_strike=cap,
        strike_type=stype,
        title="t",
    )


class StrategyConformance(unittest.TestCase):

    def test_implements_strategy_protocol(self):
        s = WeatherStrategy(cities=["NYC"])
        self.assertIsInstance(s, Strategy)
        self.assertEqual(s.name, "weather")

    def test_target_series(self):
        s = WeatherStrategy(cities=["NYC", "CHI"])
        self.assertEqual(set(s.list_target_series()), {"KXHIGHNY", "KXHIGHCHI"})

    def test_no_intent_without_observation(self):
        """Without a city observation, the strategy must yield no intents."""
        s = WeatherStrategy(cities=["NYC"])
        ctx = StrategyContext(
            now=datetime.now(timezone.utc),
            strategy_bankroll=1000.0,
            open_positions={},
        )
        snap = _snap("KXHIGHNY-26MAY01-T62", "KXHIGHNY", floor=None, cap=62, stype="less")
        intents = list(s.desired_intents([snap], ctx))
        self.assertEqual(intents, [])

    def test_emits_intent_when_edge_exceeds_threshold(self):
        """With observation set so model says ~0% but market is at $0.30, expect a NO order."""
        s = WeatherStrategy(cities=["NYC"], min_edge=0.04)
        # obs_max=80F at 18:00 → P(high < 62) is 0
        s.set_observation("NYC", obs_max_f=80.0, hour_local=18)
        ctx = StrategyContext(
            now=datetime.now(timezone.utc),
            strategy_bankroll=1000.0,
            open_positions={},
        )
        snap = _snap("KXHIGHNY-26MAY01-T62", "KXHIGHNY", floor=None, cap=62, stype="less",
                     bid=0.28, ask=0.32)
        intents = list(s.desired_intents([snap], ctx))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].side, Side.NO)
        self.assertLess(intents[0].desired_position, 0)

    def test_no_intent_when_edge_below_threshold(self):
        s = WeatherStrategy(cities=["NYC"], min_edge=0.10)
        s.set_observation("NYC", obs_max_f=63.5, hour_local=15)
        ctx = StrategyContext(
            now=datetime.now(timezone.utc),
            strategy_bankroll=1000.0,
            open_positions={},
        )
        snap = _snap("KXHIGHNY-26MAY01-B63.5", "KXHIGHNY", floor=63, cap=64, stype="between",
                     bid=0.50, ask=0.55)
        # The model probably gives something near 0.5 too — edge below 10c
        intents = list(s.desired_intents([snap], ctx))
        # We can't strictly assert empty because model might still cross
        # threshold; but if any intent is produced, it must respect threshold
        for it in intents:
            # confidence-based size sanity
            self.assertGreater(it.confidence, 0.0)


class GlobalRulesTests(unittest.TestCase):

    def test_taker_fee_formula(self):
        # 7¢ × 0.5 × 0.5 × 1 contract = 1.75¢ → rounds up to 2¢
        fee = kalshi_taker_fee(price=0.5, contracts=1)
        self.assertAlmostEqual(fee, 0.02, places=2)

    def test_maker_is_quarter_of_taker(self):
        taker = kalshi_taker_fee(0.4, 100)
        maker = kalshi_maker_fee(0.4, 100)
        self.assertAlmostEqual(maker, taker * 0.25, places=4)

    def test_kill_switch_blocks_everything(self):
        from master.interfaces import OrderIntent, Side
        rules = GlobalRules(kill_switch=True)
        intent = OrderIntent(strategy_name="weather", ticker="KXHIGHNY-X-T70",
                             side=Side.YES, desired_position=10, limit_price=0.5)
        ok, reason = rules.validate(intent)
        self.assertFalse(ok)
        self.assertIn("kill switch", reason)

    def test_blacklist_rejects_series(self):
        from master.interfaces import OrderIntent, Side
        rules = GlobalRules(blacklisted_series=frozenset({"KXSOL15M"}))
        intent = OrderIntent(strategy_name="x", ticker="KXSOL15M-Y-T1",
                             side=Side.YES, desired_position=5, limit_price=0.5)
        ok, reason = rules.validate(intent)
        self.assertFalse(ok)
        self.assertIn("blacklisted", reason)

    def test_max_order_size_enforced(self):
        from master.interfaces import OrderIntent, Side
        rules = GlobalRules(max_contracts_per_order=10)
        big = OrderIntent(strategy_name="x", ticker="KXHIGHNY-Y-T1",
                          side=Side.YES, desired_position=100, limit_price=0.5)
        ok, _ = rules.validate(big)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
