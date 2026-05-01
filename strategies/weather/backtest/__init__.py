"""Backtester for the weather strategy.

v1 scope: model-quality backtest. Replays historical daily observations,
runs the posterior at each hour, scores the predicted distribution
against the realized daily high. NOT yet a trading-P&L backtest — that
requires Kalshi orderbook history which we don't have stored.
"""
