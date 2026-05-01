"""Conformance + integration tests for EthStrategy."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from master.interfaces import MarketSnapshot, Side, Strategy, StrategyContext
from strategies.eth.strategy import EthStrategy


def _snap(strike: float, bid=0.30, ask=0.32) -> MarketSnapshot:
    ticker = f"KXETHD-26MAY0117-T{strike}"
    return MarketSnapshot(
        ticker=ticker,
        event_ticker="KXETHD-26MAY0117",
        series_ticker="KXETHD",
        yes_bid=bid,
        yes_ask=ask,
        yes_bid_size=100,
        yes_ask_size=100,
        last_price=(bid + ask) / 2,
        volume_24h=1000.0,
        open_interest=500.0,
        close_time=datetime.now(timezone.utc),
        floor_strike=strike,
        cap_strike=None,
        strike_type="greater",
        title=f"ETH > {strike}",
    )


def _ctx() -> StrategyContext:
    return StrategyContext(
        now=datetime.now(timezone.utc),
        strategy_bankroll=1000.0,
        open_positions={},
    )


class StrategyConformance(unittest.TestCase):

    def test_implements_strategy_protocol(self):
        s = EthStrategy()
        self.assertIsInstance(s, Strategy)
        self.assertEqual(s.name, "eth")

    def test_target_series(self):
        s = EthStrategy()
        self.assertIn("KXETHD", list(s.list_target_series()))

    def test_no_intent_without_state(self):
        s = EthStrategy()
        intents = list(s.desired_intents([_snap(2200.0)], _ctx()))
        self.assertEqual(intents, [])

    def test_emits_yes_when_model_above_market(self):
        # spot=2300, very low vol → very high P(price > 2200) ≈ 1.0
        # market quote bid=0.30 ask=0.32 — model says ~100% but market 31% mid → +69¢ edge
        s = EthStrategy(min_edge=0.05)
        s.set_market_state(
            "KXETHD",
            spot=2300.0,
            sigma_hourly=0.005,
            hours_to_settle=1.0,
        )
        intents = list(s.desired_intents([_snap(2200.0, bid=0.30, ask=0.32)], _ctx()))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].side, Side.YES)
        self.assertGreater(intents[0].desired_position, 0)

    def test_emits_no_when_model_below_market(self):
        # spot=2300, low vol, strike=2400 (above spot) → low prob
        # market 31% mid → model says ~0% → 31¢ negative edge → buy NO
        s = EthStrategy(min_edge=0.05)
        s.set_market_state(
            "KXETHD",
            spot=2300.0,
            sigma_hourly=0.001,  # vanishingly small
            hours_to_settle=1.0,
        )
        intents = list(s.desired_intents([_snap(2400.0, bid=0.30, ask=0.32)], _ctx()))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].side, Side.NO)
        self.assertLess(intents[0].desired_position, 0)

    def test_no_intent_when_state_invalid(self):
        s = EthStrategy()
        # zero/negative inputs → return None fair-value, no intent
        s.set_market_state("KXETHD", spot=0.0, sigma_hourly=0.01, hours_to_settle=1.0)
        self.assertEqual(list(s.desired_intents([_snap(2200.0)], _ctx())), [])
        s.set_market_state("KXETHD", spot=2300.0, sigma_hourly=0.0, hours_to_settle=1.0)
        self.assertEqual(list(s.desired_intents([_snap(2200.0)], _ctx())), [])
        s.set_market_state("KXETHD", spot=2300.0, sigma_hourly=0.01, hours_to_settle=0.0)
        self.assertEqual(list(s.desired_intents([_snap(2200.0)], _ctx())), [])


if __name__ == "__main__":
    unittest.main()
