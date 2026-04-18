# BTC Futures Bot

This is a separate BTC-only MEXC futures project added alongside the spot bot.

It is intentionally isolated from the live spot runtime:

- separate margin budget
- separate calibration and daily review files
- separate runtime state and status files
- separate backtest process

## Strategy

The strategy only trades BTC perpetual futures and only when the setup is strong enough in one direction.

- Uses 15m candles for consolidation and breakout context
- Uses 1h resampled structure for higher-timeframe trend strength
- Can open both long and short
- Dynamically sizes leverage between x20 and x50 from setup certainty
- Rejects setups where the stop distance would violate the configured hard loss cap on margin
- Uses full-size exits only
- Lets exchange TP/SL manage the hard exit path
- Adds an hourly early-take-profit check when price is already very close to TP

## Environment

Important variables:

- `FUTURES_PAPER_TRADE=true`
- `FUTURES_SYMBOL=BTC_USDT`
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
- `FUTURES_CALIBRATION_MIN_TOTAL_TRADES=4`
- `FUTURES_CALIBRATION_FILE=Futures-bot/backtest_output/calibration.json`
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