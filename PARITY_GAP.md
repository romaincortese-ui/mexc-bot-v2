# Crypto Bot Parity Gap

Source of truth for advanced behavior: `c:/Users/Rocot/Downloads/main.py`

## Ported into the new architecture

- Modular config/env parsing in `mexcbot/config.py`
- Shared indicator helpers in `mexcbot/indicators.py`
- Exchange client extraction in `mexcbot/exchange.py`
- Thin live entrypoint in `bot.py`
- Extracted strategy modules for `SCALPER`, `GRID`, `TRINITY`, `MOONSHOT`, `REVERSAL`, and `PRE_BREAKOUT`
- Lightweight backtest scaffold in `backtest/`
- Backtest execution modeling for maker/taker fees, slippage, partial fills, maker-exit fallback, and incomplete-close accounting in `backtest/engine.py`
- Synthetic exchange-style backtest behavior for defensive-close unlock delays, retry attempts, and close verification in `backtest/exchange_simulator.py` and `backtest/engine.py`
- Synthetic fill-history reconciliation for backtest entry and exit accounting in `backtest/exchange_simulator.py` and `backtest/engine.py`
- Synthetic dust-sweep timing and delayed dust settlement in `backtest/exchange_simulator.py` and `backtest/engine.py`
- Telegram command/reporting surface in `mexcbot/runtime.py` and transport in `mexcbot/telegram.py`
- Multi-position live runtime state via `open_trades`
- Runtime state persistence for `open_trades`, trade history, cooldowns, pause guards, and equity anchors via `MEXCBOT_STATE_FILE`
- Moonshot maximum-volume gating now matches the monolith's balance-scaled `MOONSHOT_MAX_VOL_RATIO` behavior, including a small-account floor
- Cached Anthropic web-search buzz scoring for near-threshold moonshot candidates via `WEB_SEARCH_ENABLED`, `MOONSHOT_SOCIAL_BOOST_MAX`, and `MOONSHOT_SOCIAL_CACHE_MINS`
- Profit-oriented buzz weighting: only a limited number of near-threshold moonshot candidates trigger buzz lookups per scan, and any buzz boost is scaled by technical quality rather than added raw
- Profit-oriented moonshot pruning: social input no longer creates a trend-entry lane, non-new rebound entries are skipped, and momentum breakouts are disabled by default so moonshot concentrates on higher-quality continuation setups
- Deterministic backtest moonshot social proxy in `backtest/moonshot_proxy.py`, so exact-window backtests can validate trending/buzz-sensitive moonshot behavior without live web-search dependencies
- Strategy capital pools, session/scalper pause guards, strategy cooldowns, adaptive thresholds, Kelly-based scalper sizing, budget rebalancing, BTC regime gating, liquidity blacklisting, and locked-balance-aware close sizing in `mexcbot/runtime.py`
- Partial-close support and strategy-aware exit profiles in `mexcbot/exits.py`
- Dust-aware close handling, daily dust sweep conversion, cancel-before-close verification, and chase-limit profit exits in `mexcbot/runtime.py` and `mexcbot/exchange.py`
- Maker-first entry flow, exchange TP order placement, and TP fill / major-partial-fill handling for `SCALPER`, `TRINITY`, and `GRID`
- Chase partial-fill fallback to market sell for non-defensive exits
- Scalper candidate correlation filtering
- Tests for indicators, strategies, reporter, backtest engine, and runtime accounting

## Remaining parity boundary

### Live-only runtime behavior

- Fill sourcing from real exchange history (`myTrades`/resolved order data) rather than synthetic backtest fill records

This remaining behavior exists in the live runtime, but it depends on exchange-side data sources that the backtester cannot reproduce directly.

## Recommended next porting order

1. Decide whether the current CoinGecko plus Anthropic social stack is sufficient, or whether you want to port the monolith's exact scraping prompts while keeping the new quality-weighted buzz ranking.
2. Decide whether synthetic fill reconciliation is sufficient for parity, or whether you want to keep pursuing a live-history replay model for research purposes.
3. Keep docs and Railway env mappings aligned with the scoped `pytest` setup and the nested `FX-bot` repo boundary.