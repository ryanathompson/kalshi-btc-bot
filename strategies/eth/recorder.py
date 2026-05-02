"""Live book recorder — Kalshi KXETHD + Coinbase ETH-USD spot.

Polls both feeds at a fixed interval and appends one jsonl record per
poll. The captured stream lets us measure ex-post:

  - **Lag**: at time t, what was the Kalshi mid-vs-Coinbase implied-mid
    delta, and how does that delta evolve when spot moves fast?
  - **Edge**: would the model's fair value at time t have produced a
    profitable order against the Kalshi book observed at time t?

Two record kinds, both with absolute UTC timestamps:

  ``{"t": 1716000000.123, "kind": "spot", "product": "ETH-USD", "price": 2300.45}``

  ``{"t": 1716000000.456, "kind": "kalshi_book", "event_ticker": "...", "markets": [...]}``

Run::

    python -m strategies.eth.recorder \
        --series KXETHD --product ETH-USD \
        --interval-seconds 5 --out logs/eth_book.jsonl

Stop with Ctrl-C — the file is flushed after each record so partial
captures are safe.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from master.kalshi_public import KalshiPublicReader
from strategies.eth.data.coinbase import CoinbaseClient


@dataclass(frozen=True)
class SpotRecord:
    t: float
    kind: str  # "spot"
    product: str
    price: float


@dataclass(frozen=True)
class BookSnapshot:
    """Top of book for one Kalshi market."""

    ticker: str
    strike: Optional[float]
    strike_type: str
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float
    last_price: float


@dataclass(frozen=True)
class BookRecord:
    t: float
    kind: str  # "kalshi_book"
    series: str
    event_ticker: str
    markets: list[dict]


class Recorder:
    """Loops polls of both feeds, writes jsonl."""

    def __init__(
        self,
        out: TextIO,
        series_ticker: str,
        coinbase_product: str,
        interval_seconds: float,
        kalshi_reader: Optional[KalshiPublicReader] = None,
        coinbase_client: Optional[CoinbaseClient] = None,
    ) -> None:
        self.out = out
        self.series_ticker = series_ticker
        self.coinbase_product = coinbase_product
        self.interval_seconds = interval_seconds
        self.kalshi = kalshi_reader or KalshiPublicReader()
        self.coinbase = coinbase_client or CoinbaseClient()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def write_record(self, rec: dict) -> None:
        self.out.write(json.dumps(rec, separators=(",", ":")))
        self.out.write("\n")
        self.out.flush()

    def poll_spot(self) -> Optional[SpotRecord]:
        price = self.coinbase.fetch_spot(self.coinbase_product)
        if price is None:
            return None
        return SpotRecord(
            t=time.time(),
            kind="spot",
            product=self.coinbase_product,
            price=price,
        )

    def poll_kalshi_book(self) -> Optional[BookRecord]:
        # Find the next-to-settle active event for this series
        events = self.kalshi.list_events(self.series_ticker, status="open", limit=10)
        if not events:
            return None
        # Sort by strike_date ascending; pick the soonest
        events_sorted = sorted(events, key=lambda e: e.strike_date)
        target_event = events_sorted[0]
        markets = self.kalshi.list_markets(event_ticker=target_event.event_ticker)
        snapshots = []
        for m in markets:
            snapshots.append({
                "ticker": m.ticker,
                "strike": m.floor_strike if m.floor_strike is not None else m.cap_strike,
                "strike_type": m.strike_type,
                "yes_bid": m.yes_bid,
                "yes_ask": m.yes_ask,
                "yes_bid_size": m.yes_bid_size,
                "yes_ask_size": m.yes_ask_size,
                "last": m.last_price,
            })
        return BookRecord(
            t=time.time(),
            kind="kalshi_book",
            series=self.series_ticker,
            event_ticker=target_event.event_ticker,
            markets=snapshots,
        )

    def tick(self) -> tuple[Optional[SpotRecord], Optional[BookRecord]]:
        """One poll cycle. Returns (spot, book), both possibly None on errors."""
        spot = book = None
        try:
            spot = self.poll_spot()
            if spot:
                self.write_record(asdict(spot))
        except Exception as e:
            self._log_error("spot poll failed", e)
        try:
            book = self.poll_kalshi_book()
            if book:
                self.write_record(asdict(book))
        except Exception as e:
            self._log_error("kalshi poll failed", e)
        return spot, book

    def _log_error(self, msg: str, e: Exception) -> None:
        rec = {
            "t": time.time(),
            "kind": "error",
            "msg": msg,
            "err": f"{type(e).__name__}: {e}",
        }
        try:
            self.write_record(rec)
        except Exception:
            pass

    def run(self) -> None:
        """Main loop. Returns when ``stop()`` is called or interrupted."""
        while not self._stop:
            cycle_start = time.monotonic()
            self.tick()
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, self.interval_seconds - elapsed)
            # Sleep in 0.1s chunks so SIGINT is responsive
            slept = 0.0
            while slept < sleep_for and not self._stop:
                chunk = min(0.1, sleep_for - slept)
                time.sleep(chunk)
                slept += chunk


def _install_sigint(recorder: Recorder) -> None:
    def handler(signum, frame):
        print("\n[recorder] SIGINT — stopping after current cycle")
        recorder.stop()
    signal.signal(signal.SIGINT, handler)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Live ETH book + spot recorder")
    parser.add_argument("--series", default="KXETHD", help="Kalshi series ticker (default: KXETHD)")
    parser.add_argument("--product", default="ETH-USD", help="Coinbase product id (default: ETH-USD)")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="Poll interval per cycle (default: 5.0)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output jsonl file (default: stdout)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop after N cycles (0 = run forever; useful for tests)",
    )
    args = parser.parse_args(argv)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_file = out_path.open("a", buffering=1)
    else:
        out_file = sys.stdout

    recorder = Recorder(
        out=out_file,
        series_ticker=args.series,
        coinbase_product=args.product,
        interval_seconds=args.interval_seconds,
    )
    _install_sigint(recorder)

    print(f"[recorder] series={args.series} product={args.product} "
          f"interval={args.interval_seconds}s out={args.out or 'stdout'}")
    if args.max_cycles > 0:
        for _ in range(args.max_cycles):
            if recorder._stop:
                break
            recorder.tick()
            time.sleep(args.interval_seconds)
    else:
        recorder.run()

    if args.out:
        out_file.close()
    print("[recorder] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
