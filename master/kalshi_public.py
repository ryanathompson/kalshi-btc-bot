"""Read-only Kalshi public-API adapter — shared across strategies.

Uses ``api.elections.kalshi.com/trade-api/v2/`` endpoints which require
no authentication for read operations. Used by strategies for market
enumeration / book reading. Order placement is master's job (TBD), not
this module's.

Why in master/: every strategy needs to read Kalshi market metadata.
Per the architectural rule, strategies don't import from each other,
so the common reader lives in master.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class KalshiEvent:
    """A single Kalshi event (one tradeable scenario, e.g. one settlement instance)."""

    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str
    strike_date: datetime  # close time, UTC


@dataclass(frozen=True)
class KalshiMarket:
    """A single Kalshi market within an event."""

    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float
    last_price: float
    volume_24h: float
    open_interest: float
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    strike_type: str  # "greater" | "less" | "between"
    raw: Mapping[str, object] = field(default_factory=dict)


class KalshiPublicReader:
    """Read-only client for Kalshi public market data.

    No retry/backoff for now — Kalshi's public-data endpoints don't
    rate-limit aggressively. Add backoff here if/when we see 429s.
    """

    def __init__(
        self,
        session: Optional["requests.Session"] = None,
        timeout: float = 15.0,
    ) -> None:
        if requests is None:
            raise ImportError("requests is required; pip install requests")
        self.session = session or requests.Session()
        self.timeout = timeout

    def list_events(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 50,
    ) -> list[KalshiEvent]:
        params = {"series_ticker": series_ticker, "status": status, "limit": limit}
        url = f"{KALSHI_API_BASE}/events?{urlencode(params)}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        out: list[KalshiEvent] = []
        for ev in payload.get("events") or []:
            strike_date = _parse_iso(ev.get("strike_date") or ev.get("close_time"))
            out.append(KalshiEvent(
                event_ticker=ev.get("event_ticker", ""),
                series_ticker=ev.get("series_ticker", series_ticker),
                title=ev.get("title", ""),
                sub_title=ev.get("sub_title", ""),
                strike_date=strike_date or datetime.now(timezone.utc),
            ))
        return out

    def list_markets(
        self,
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 200,
    ) -> list[KalshiMarket]:
        """List markets filtered by series OR event."""
        if not (series_ticker or event_ticker):
            raise ValueError("must provide series_ticker or event_ticker")
        params: dict[str, object] = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        url = f"{KALSHI_API_BASE}/markets?{urlencode(params)}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        out: list[KalshiMarket] = []
        for m in payload.get("markets") or []:
            out.append(KalshiMarket(
                ticker=m.get("ticker", ""),
                event_ticker=m.get("event_ticker", ""),
                series_ticker=m.get("series_ticker", series_ticker or ""),
                title=m.get("title", ""),
                yes_bid=_to_float(m.get("yes_bid_dollars")),
                yes_ask=_to_float(m.get("yes_ask_dollars")),
                yes_bid_size=_to_float(m.get("yes_bid_size_fp")),
                yes_ask_size=_to_float(m.get("yes_ask_size_fp")),
                last_price=_to_float(m.get("last_price_dollars")),
                volume_24h=_to_float(m.get("volume_24h_fp")),
                open_interest=_to_float(m.get("open_interest_fp")),
                floor_strike=_optional_float(m.get("floor_strike")),
                cap_strike=_optional_float(m.get("cap_strike")),
                strike_type=m.get("strike_type", ""),
                raw=m,
            ))
        return out


def _to_float(v: object) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(v: object) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
