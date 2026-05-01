"""Backtester for the ETH strategy.

v1 scope: model-quality backtest. Replays historical hourly candles,
estimates volatility from a trailing window, applies the GBM posterior
to predict the price 1 hour out, and scores predicted bracket
probabilities against realized prices. Trading-P&L backtest is v2,
blocked on Kalshi orderbook history.
"""
