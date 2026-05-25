"""MarsWalk — walk-forward resilience backtester for the option trader.

Replays historical regimes through the SHARED option-selection cores
(src.strategy.option_scoring) in a fully isolated sandbox. Uses its own DB
(data/marswalk.db) and never touches trades.db or the live IBKR connection.
"""
