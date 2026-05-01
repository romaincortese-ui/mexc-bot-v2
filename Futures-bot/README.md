# MEXC Futures Bot

This is a separate MEXC perpetual-futures project added alongside the spot bot.

It is intentionally isolated from the live spot runtime:

- separate margin budget
- separate calibration and daily review files
- separate runtime state and status files
- separate backtest process

## Strategy

The default production universe scans 10 perpetual pairs:

```text
BTC_USDT, ETH_USDT, SOL_USDT, PEPE_USDT, TAO_USDT,
BNB_USDT, BCH_USDT, SEI_USDT, LINK_USDT, ZEC_USDT
```

Each pair uses the shared futures scorer with a dedicated profile for volatility, funding, score threshold, reward/risk, and leverage cap. A packaged signal-lane calibration in `calibration/multi_symbol_calibration.json` blocks symbol/signal combinations that were persistently negative in the latest 60-day replay, so the bot can scan broadly without treating every pair like BTC.

- Uses 15m candles for consolidation and breakout context
- Uses 1h resampled structure for higher-timeframe trend strength
- Can open both long and short
- Dynamically sizes leverage between x20 and x50 from setup certainty
- Rejects setups where the stop distance would violate the configured hard loss cap on margin
- Uses full-size exits only
- Lets exchange TP/SL manage the hard exit path
- Adds an hourly early-take-profit check when price is already very close to TP
- Applies per-symbol profiles and calibration blocks before entry

## Environment

Important variables:

- `FUTURES_PAPER_TRADE=true`
- `FUTURES_SYMBOLS=BTC_USDT,ETH_USDT,SOL_USDT,PEPE_USDT,TAO_USDT,BNB_USDT,BCH_USDT,SEI_USDT,LINK_USDT,ZEC_USDT`
- `FUTURES_SYMBOL=BTC_USDT` for a one-symbol run or single-symbol backtest
- `FUTURES_MARGIN_BUDGET_USDT=75`
- `FUTURES_TELEGRAM_TOKEN=...`
- `FUTURES_TELEGRAM_CHAT_ID=...`
- `FUTURES_HEARTBEAT_SECONDS=3600`
- `FUTURES_SCORE_THRESHOLD=56`
- `FUTURES_LEVERAGE_MIN=20`
- `FUTURES_LEVERAGE_MAX=50`
- `FUTURES_HARD_LOSS_CAP_PCT=0.75`
- `FUTURES_ADX_FLOOR=18`
- `FUTURES_TREND_24H_FLOOR=0.009`
- `FUTURES_TREND_6H_FLOOR=0.003`
- `FUTURES_VOLUME_RATIO_FLOOR=1.0`
- `FUTURES_MIN_REWARD_RISK=1.15`
- `FUTURES_CALIBRATION_MIN_TOTAL_TRADES=15`
- `FUTURES_CALIBRATION_FILE=Futures-bot/calibration/multi_symbol_calibration.json`
- `FUTURES_CALIBRATION_OUTPUT_FILE=Futures-bot/backtest_output/calibration.json`
- `FUTURES_DAILY_REVIEW_FILE=Futures-bot/backtest_output/daily_review.json`

The project reuses `MEXC_API_KEY`, `MEXC_API_SECRET`, `REDIS_URL`, and `ANTHROPIC_API_KEY` when present.

If you do not set `FUTURES_TELEGRAM_TOKEN` or `FUTURES_TELEGRAM_CHAT_ID`, the runtime falls back to `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

## Run Live Runtime

```bash
python Futures-bot/main.py
```

The runtime now sends Telegram notifications for:

- startup
- hourly heartbeat/status
- new position opened
- position closed
- loop errors with cooldown protection

Supported Telegram commands:

- `/status`
- `/pnl`
- `/logs`
- `/pause`
- `/resume`
- `/close`
- `/help`

## Run 60-Day Backtest

```bash
python Futures-bot/run_backtest.py
```

Rolling-window runs default to 60 days. You can override the window with:

- `FUTURES_BACKTEST_START`
- `FUTURES_BACKTEST_END`
- `FUTURES_BACKTEST_ROLLING_DAYS`

## Run Daily Calibration + AI Review

```bash
python Futures-bot/run_daily_calibration.py
```

This writes:

- `Futures-bot/backtest_output/summary.json`
- `Futures-bot/backtest_output/calibration.json`
- `Futures-bot/backtest_output/daily_review.json`

If Redis is configured, it also publishes the calibration and review payloads for the runtime to consume on the next loop.

## Run Multi-Symbol Replay

```powershell
Set-Location Futures-bot
$env:PYTHONPATH=(Get-Location).Path
..\.venv\Scripts\python.exe tools/run_multi_symbol_backtest.py --start 2026-03-02 --end 2026-05-01 --mode both
```

Use `USE_REALISTIC_BACKTEST=1`, `REALISTIC_FUNDING_RATE_8H`, `REALISTIC_SLIPPAGE_BPS_PER_LEV`, and `REALISTIC_EXIT_SLIP_MULT` to include conservative funding and slippage assumptions.