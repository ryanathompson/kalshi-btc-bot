"""Tests for the ETH book recorder.

Mocks both HTTP fetchers so the test runs offline. Confirms:
  - Each poll produces valid jsonl
  - Errors in either feed don't kill the recorder
  - Selected event is the soonest-to-settle
"""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from master.kalshi_public import KalshiEvent, KalshiMarket
from strategies.eth.recorder import Recorder


def _fake_event(ticker: str, hours_from_now: int) -> KalshiEvent:
    return KalshiEvent(
        event_ticker=ticker,
        series_ticker="KXETHD",
        title=f"ETH price at {ticker}",
        sub_title="",
        strike_date=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
    )


def _fake_market(ticker: str, strike: float, bid=0.45, ask=0.47) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="KXETHD-26MAY0117",
        series_ticker="KXETHD",
        title="",
        yes_bid=bid,
        yes_ask=ask,
        yes_bid_size=100,
        yes_ask_size=50,
        last_price=(bid + ask) / 2,
        volume_24h=0.0,
        open_interest=0.0,
        floor_strike=strike,
        cap_strike=None,
        strike_type="greater",
    )


class RecorderTickTests(unittest.TestCase):

    def _build(self, *, spot=2300.45, events=None, markets=None) -> tuple[Recorder, io.StringIO]:
        kalshi = MagicMock()
        kalshi.list_events.return_value = (
            [_fake_event("KXETHD-LATER", 5), _fake_event("KXETHD-SOON", 1)]
            if events is None
            else events
        )
        kalshi.list_markets.return_value = (
            [_fake_market("KXETHD-SOON-T2300.0", 2300.0),
             _fake_market("KXETHD-SOON-T2340.0", 2340.0, bid=0.30, ask=0.33)]
            if markets is None
            else markets
        )
        coinbase = MagicMock()
        coinbase.fetch_spot.return_value = spot
        out = io.StringIO()
        rec = Recorder(
            out=out,
            series_ticker="KXETHD",
            coinbase_product="ETH-USD",
            interval_seconds=0.0,
            kalshi_reader=kalshi,
            coinbase_client=coinbase,
        )
        return rec, out

    def test_tick_writes_two_records(self):
        rec, out = self._build()
        rec.tick()
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        recs = [json.loads(l) for l in lines]
        kinds = {r["kind"] for r in recs}
        self.assertEqual(kinds, {"spot", "kalshi_book"})

    def test_picks_soonest_event(self):
        rec, out = self._build()
        rec.tick()
        book = next(json.loads(l) for l in out.getvalue().splitlines()
                    if json.loads(l)["kind"] == "kalshi_book")
        self.assertEqual(book["event_ticker"], "KXETHD-SOON")
        # Two markets in the snapshot
        self.assertEqual(len(book["markets"]), 2)
        # Strike + book fields preserved
        m = book["markets"][0]
        self.assertIn("ticker", m)
        self.assertIn("yes_bid", m)
        self.assertIn("yes_ask", m)
        self.assertEqual(m["strike_type"], "greater")

    def test_spot_record_shape(self):
        rec, out = self._build(spot=2305.5)
        rec.tick()
        spot = next(json.loads(l) for l in out.getvalue().splitlines()
                    if json.loads(l)["kind"] == "spot")
        self.assertEqual(spot["product"], "ETH-USD")
        self.assertEqual(spot["price"], 2305.5)
        self.assertIn("t", spot)

    def test_spot_failure_doesnt_block_book(self):
        kalshi = MagicMock()
        kalshi.list_events.return_value = [_fake_event("KXETHD-X", 1)]
        kalshi.list_markets.return_value = [_fake_market("KXETHD-X-T2300.0", 2300.0)]
        coinbase = MagicMock()
        coinbase.fetch_spot.side_effect = RuntimeError("network down")
        out = io.StringIO()
        rec = Recorder(
            out=out,
            series_ticker="KXETHD",
            coinbase_product="ETH-USD",
            interval_seconds=0.0,
            kalshi_reader=kalshi,
            coinbase_client=coinbase,
        )
        rec.tick()
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        kinds = [json.loads(l)["kind"] for l in lines]
        self.assertIn("error", kinds)
        self.assertIn("kalshi_book", kinds)

    def test_kalshi_failure_doesnt_block_spot(self):
        kalshi = MagicMock()
        kalshi.list_events.side_effect = RuntimeError("kalshi down")
        coinbase = MagicMock()
        coinbase.fetch_spot.return_value = 2300.0
        out = io.StringIO()
        rec = Recorder(
            out=out,
            series_ticker="KXETHD",
            coinbase_product="ETH-USD",
            interval_seconds=0.0,
            kalshi_reader=kalshi,
            coinbase_client=coinbase,
        )
        rec.tick()
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        kinds = [json.loads(l)["kind"] for l in lines]
        self.assertIn("spot", kinds)
        self.assertIn("error", kinds)

    def test_no_events_returns_no_book(self):
        rec, out = self._build(events=[])
        rec.tick()
        kinds = [json.loads(l)["kind"] for l in out.getvalue().splitlines() if l.strip()]
        self.assertIn("spot", kinds)
        self.assertNotIn("kalshi_book", kinds)


if __name__ == "__main__":
    unittest.main()
