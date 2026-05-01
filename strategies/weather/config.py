"""City and series configuration for the weather strategy.

The (city, series, ASOS station, timezone) mapping is the single source
of truth for which markets the strategy targets. Update this file (and
verify against the Kalshi market ``rules_primary`` text) when adding
new cities.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CityConfig:
    code: str            # short id used in CLI flags, e.g. "NYC"
    kalshi_series: str   # Kalshi series ticker, e.g. "KXHIGHNY"
    asos_station: str    # IEM ASOS 3-letter station id, e.g. "NYC"
    asos_network: str    # IEM network, typically "<state>_ASOS", e.g. "NY_ASOS"
    timezone: str        # IANA tz, used for daily-window math
    display_name: str    # human-friendly, e.g. "New York (Central Park)"


CITIES: dict[str, CityConfig] = {
    "NYC": CityConfig(
        code="NYC",
        kalshi_series="KXHIGHNY",
        asos_station="NYC",
        asos_network="NY_ASOS",
        timezone="America/New_York",
        display_name="New York (Central Park)",
    ),
    "CHI": CityConfig(
        code="CHI",
        kalshi_series="KXHIGHCHI",
        asos_station="ORD",
        asos_network="IL_ASOS",
        timezone="America/Chicago",
        display_name="Chicago (O'Hare)",
    ),
    "MIA": CityConfig(
        code="MIA",
        kalshi_series="KXHIGHMIA",
        asos_station="MIA",
        asos_network="FL_ASOS",
        timezone="America/New_York",
        display_name="Miami International",
    ),
    "LAX": CityConfig(
        code="LAX",
        kalshi_series="KXHIGHLAX",
        asos_station="LAX",
        asos_network="CA_ASOS",
        timezone="America/Los_Angeles",
        display_name="Los Angeles International",
    ),
    "DEN": CityConfig(
        code="DEN",
        kalshi_series="KXHIGHDEN",
        asos_station="DEN",
        asos_network="CO_ASOS",
        timezone="America/Denver",
        display_name="Denver International",
    ),
    "AUS": CityConfig(
        code="AUS",
        kalshi_series="KXHIGHAUS",
        asos_station="AUS",
        asos_network="TX_ASOS",
        timezone="America/Chicago",
        display_name="Austin-Bergstrom",
    ),
}


def get_city(code: str) -> CityConfig:
    """Look up a city config by 3-letter code (case-insensitive)."""
    key = code.upper()
    if key not in CITIES:
        known = ", ".join(sorted(CITIES))
        raise KeyError(f"Unknown city code {code!r}; known: {known}")
    return CITIES[key]
