"""
strategy_prefixes.py — Single source of truth for client_order_id prefix → strategy.

KalshiClient.place_order() builds the client_order_id prefix as
    (strategy_tag or "UNK")[:3].upper()
i.e. the first three letters of the strategy name. KalshiClient.sell_order()
overrides this to "EXT" for early-exit sells (so EARLY_EXIT does NOT use the
first-three-letters convention — it's an explicit override).

Three readers depend on inverting this mapping:
  - bot.py            rebuild_trades_from_api()  (post-redeploy attribution recovery)
  - reconcile_pnl.py  compute_true_pnl()         (Kalshi-API ground truth, daily snapshot)
  - analyze_edge.py                              (offline analysis tooling)

Previously each held its own copy and the three drifted. SNI (SNIPER) and
EXP (EXPIRY_DECAY) were missing in all three — so reconcile snapshots
bucketed every SNIPER and EXPIRY_DECAY fill as UNKNOWN, undercounting
both strategies' contribution to P&L. EXT was missing in reconcile and
analyze_edge, breaking early-exit attribution there.

Add new strategies to PREFIX_TO_STRATEGY here, NOT at the call sites.
"""

PREFIX_TO_STRATEGY: dict[str, str] = {
    "LAG": "LAG",
    "CON": "CONSENSUS",     # also covers CONSENSUS_V2 (same 3-letter prefix)
    "SNI": "SNIPER",
    "EXP": "EXPIRY_DECAY",
    "EXT": "EARLY_EXIT",    # explicit override from KalshiClient.sell_order()
    "BRI": "BRIDGE",        # beta — emitted whenever BRIDGE_LIVE_STAKE > 0
    "FAD": "FADE",          # beta — same shape if FADE ever goes live
    "TAI": "TAIL",          # legacy — no live strategy emits this anymore;
                            # kept so historical TAI-prefixed fills still
                            # attribute correctly.
}
