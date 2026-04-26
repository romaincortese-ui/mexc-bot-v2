# MEXC Bot 2

A clean modular MEXC crypto bot with a live runner and a matching backtest scaffold. This folder is the extraction target only: no copied advanced runtime, no runtime dispatcher, just the new architecture.

The runtime now supports a bounded portfolio instead of a single position, and the extracted exit engine now includes partial take profit plus floor/chase behavior for strategies that need a runner-management path.

---

## Strategy Overview

| Parameter | Value |
|-----------|-------|
| `SCALPER` | RSI + EMA crossover + volume confirmation on 5-minute candles |
| `GRID` | 15-minute mean reversion using Bollinger squeeze, RSI band, and low-ADX regime |
| `TRINITY` | 15-minute deep-dip recovery using multi-window drop detection and volume-backed rebound |
| `MOONSHOT` | 15-minute liquid alt momentum/rebound scanner with runner-style exit management |
| `REVERSAL` | 5-minute capitulation-bounce scanner for oversold liquid alts |
| `PRE_BREAKOUT` | 5-minute pre-breakout continuation setup with tight invalidation |
| Runtime model | Modular only |
| Shared exits | Breakeven, trailing, flat-time exits, partial TP, floor/chase |

---

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your MEXC API key (leave PAPER_TRADE=true for now)
```

Optional:

```bash
# Enables the Telegram /ask command
ANTHROPIC_API_KEY=your_anthropic_key
WEB_SEARCH_ENABLED=true
```

### 3. Run locally (paper trading)

```bash
python bot.py
```

`bot.py` runs the modular runtime directly.

You can choose which extracted strategies run:

```bash
MEXCBOT_STRATEGIES=SCALPER,GRID,TRINITY,MOONSHOT,REVERSAL,PRE_BREAKOUT
MAX_OPEN_POSITIONS=3
SCALPER_ALLOCATION_PCT=0.25
MOONSHOT_ALLOCATION_PCT=0.45
TRINITY_ALLOCATION_PCT=0.10
GRID_ALLOCATION_PCT=0.20
SCALPER_BUDGET_PCT=0.42
MOONSHOT_BUDGET_PCT=0.048
REVERSAL_BUDGET_PCT=0.12
TRINITY_BUDGET_PCT=0.20
GRID_BUDGET_PCT=0.40
MAX_CONSECUTIVE_LOSSES=4
STREAK_AUTO_RESET_MINS=60
SESSION_LOSS_PAUSE_PCT=0.03
SESSION_LOSS_PAUSE_MINS=120
MOONSHOT_SYMBOLS=SOLUSDT,DOGEUSDT,PEPEUSDT,ENAUSDT,WIFUSDT
MOONSHOT_SOCIAL_BOOST_MAX=20
MOONSHOT_SOCIAL_CACHE_MINS=20
MOONSHOT_SOCIAL_MAX_EVALS=1
MOONSHOT_MAX_RECENT_RETURN_PCT=4.5
MOONSHOT_ENABLE_MOMENTUM=false
MOONSHOT_MOMENTUM_EXTRA_SCORE=4
MOONSHOT_MOMENTUM_MIN_RETURN_PCT=1.2
MOONSHOT_MOMENTUM_MAX_RETURN_PCT=2.0
MOONSHOT_TREND_CONTINUATION_EXTRA_SCORE=8
MOONSHOT_TREND_CONTINUATION_MAX_MATURITY=0.45
MOONSHOT_MAX_VOL_RATIO=100000
REVERSAL_SYMBOLS=SOLUSDT,DOGEUSDT,ETHUSDT,PEPEUSDT,WIFUSDT
GRID_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ADAUSDT
TRINITY_SYMBOLS=SOLUSDT,ETHUSDT,DOGEUSDT,XRPUSDT
```

`MOONSHOT_MAX_VOL_RATIO` now matches the monolith semantics again: it scales the maximum eligible 24h quote volume by account size, with a built-in floor to avoid over-filtering small accounts. The moonshot strategy can still use cached Anthropic web-search buzz scoring on a very small number of near-threshold candidates when `WEB_SEARCH_ENABLED=true`, but social input no longer creates its own trend-entry lane. The current profit-oriented default is narrower than earlier moonshot iterations: non-new rebound entries are skipped, momentum breakouts are disabled by default via `MOONSHOT_ENABLE_MOMENTUM=false`, and trend continuation is kept on a tighter maturity leash with `MOONSHOT_TREND_CONTINUATION_MAX_MATURITY=0.45`. If you want to re-open the momentum lane later, keep it capped with `MOONSHOT_MOMENTUM_MIN_RETURN_PCT` and `MOONSHOT_MOMENTUM_MAX_RETURN_PCT` rather than letting it chase extended moves.

### 4. Run a backtest

```bash
python -m backtest.run_backtest --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 5m
```

Useful env knobs:

```bash
BACKTEST_ROLLING_DAYS=31
BACKTEST_END_OFFSET_HOURS=0
BACKTEST_SCALPER_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT
BACKTEST_MOONSHOT_SYMBOLS=SOLUSDT,DOGEUSDT,PEPEUSDT,ENAUSDT,WIFUSDT,BONKUSDT
BACKTEST_MAX_OPEN_POSITIONS=3
BACKTEST_REENTRY_COOLDOWN_BARS=12
BACKTEST_TRADE_BUDGET=50
BACKTEST_MAKER_FEE_RATE=0.0
BACKTEST_TAKER_FEE_RATE=0.001
BACKTEST_MAKER_SLIPPAGE_RATE=0.0002
BACKTEST_TAKER_SLIPPAGE_RATE=0.001
BACKTEST_MAKER_FILL_RATIO=1.0
BACKTEST_TAKER_FILL_RATIO=1.0
BACKTEST_SYNTHETIC_DEFENSIVE_UNLOCK_BARS=0
BACKTEST_SYNTHETIC_CLOSE_MAX_ATTEMPTS=1
BACKTEST_SYNTHETIC_RETRY_DELAY_BARS=1
BACKTEST_SYNTHETIC_DUST_THRESHOLD_USDT=3.0
BACKTEST_SYNTHETIC_CLOSE_VERIFY_RATIO=0.01
BACKTEST_SYNTHETIC_DUST_SWEEP_ENABLED=false
BACKTEST_SYNTHETIC_DUST_CONVERSION_FEE_RATE=0.0
```

`BACKTEST_MAKER_FILL_RATIO` and `BACKTEST_TAKER_FILL_RATIO` let the backtester simulate partial fills. A maker exit can now fill partially and immediately fall back to taker execution for the remainder, matching the live runtime's non-defensive close behavior more closely.

`BACKTEST_SYNTHETIC_DEFENSIVE_UNLOCK_BARS`, `BACKTEST_SYNTHETIC_CLOSE_MAX_ATTEMPTS`, and `BACKTEST_SYNTHETIC_RETRY_DELAY_BARS` enable a simple exchange-simulator layer for defensive exits. That lets backtests approximate cancel/unlock delay and repeated close attempts across later bars instead of assuming every stop-style close happens instantly.

If you also enable `BACKTEST_SYNTHETIC_DUST_SWEEP_ENABLED`, the backtester will hold dust-sized verified-close proceeds out of reusable cash until the next synthetic UTC-midnight sweep. That keeps total equity and free balance closer to the live bot's account-cleanup behavior. `BACKTEST_SYNTHETIC_DUST_CONVERSION_FEE_RATE` lets you approximate conversion drag at settlement time.

The remaining live/backtest difference is now mostly about data provenance rather than execution logic. The backtester records synthetic entry and exit fill histories, reconciles prices and fees from those fills, and can approximate dust-sweep timing, but it still does not query real exchange history the way the live runtime does.

If you set `BACKTEST_START` and `BACKTEST_END`, those explicit timestamps override the rolling window. CLI `--start` and `--end` still override both.

When the rolling window is active, the default `end` snaps to the latest fully closed candle for `BACKTEST_INTERVAL`, so a `5m` backtest ends on a clean `:00`, `:05`, `:10`, and so on.

The backtest now supports strategy-specific datasets. That matters most for `SCALPER` and `MOONSHOT`, where a BTC/ETH/SOL-only basket is not representative of the live universe. `BACKTEST_SYMBOLS` remains a shared fallback, while `BACKTEST_SCALPER_SYMBOLS`, `BACKTEST_MOONSHOT_SYMBOLS`, and the other strategy-specific env vars let you mirror each strategy's actual pair mix.

Each backtest run now also writes a crypto calibration payload to `backtest_output/calibration.json`. If `REDIS_URL` is set, it also publishes that payload to `MEXCBOT_CALIBRATION_REDIS_KEY` so the live bot can consume the same dataset.

For scheduled calibration refreshes and the daily missed-opportunity review, use:

```bash
python -m backtest.run_daily_calibration
```

That entrypoint always ignores `BACKTEST_START` and `BACKTEST_END`, so a cron job cannot accidentally keep replaying a stale fixed window. It now does two things in one run:

- reruns the rolling-window calibration and republishes it to file / Redis
- runs a separate last-24h review pass, writes `backtest_output/daily_review.json`, and publishes the review to `MEXCBOT_DAILY_REVIEW_REDIS_KEY`

If `ANTHROPIC_API_KEY` is set, the review is AI-assisted. If not, it falls back to deterministic summaries and parameter suggestions.

For interactive parameter comparisons, use:

```bash
python -m backtest.run_parameter_sweep
```

That runner compares three profiles:

- `production_anchor`
- `conservative`
- `aggressive`

It writes separate artifacts per profile under `backtest_sweeps/` plus a combined `comparison.json`.

The sweep only covers monolith variables that are currently implemented in the extracted bot. Strategy selection, entry thresholds, RSI gates, ATR multipliers, partial-take-profit ratios, and flat/breakeven timings are included. Execution-layer settings such as maker-order handling, dust cleanup, adaptive learning, and streak controls are still monolith-only and are reported as unsupported by the sweep.

The live runtime uses that calibration in three places:

- entry gating: tighten, relax, or block strategy/pair setups based on backtest results
- sizing: scale allocation per strategy/pair using the calibrated risk multiplier
- exits: overlay calibrated trail, breakeven, partial-take-profit, and flat-time adjustments on top of the default exit profile

The live runtime can also load the daily review payload and expose it over Telegram with `/review`, including suggested env-var changes such as threshold tightening or loosening when the last-24h review supports it. Supported suggestions can be applied live with `/approve <n>`, which updates the running bot and persists the override across restarts.

Artifacts are written to `backtest_output/`:

- `equity_curve.csv`
- `trade_journal.csv`
- `summary.json`
- `calibration.json`

`trade_journal.csv` now includes execution-model fields that make backtest exits easier to audit, including partial-fill outcomes, maker-to-market fallback behavior, synthetic retry-attempt metadata for delayed defensive closes, reconciled `entry_fill_history` / `exit_fill_history` records, and dust-settlement metadata when synthetic sweeps are enabled.

The report now also includes dynamic `by_strategy_signal` and `by_strategy_symbol_signal` sections so you can see whether a strategy is failing broadly or only on a specific entry path.

---

## Deploy to Railway (recommended)

Railway gives you a free-tier cloud server — your bot runs 24/7 without your laptop needing to be on.

### Steps

1. **Create account** at [railway.app](https://railway.app) (free tier works fine)

2. **Push your code to GitHub** (don't commit `.env` — it's in `.gitignore`)

```bash
git init
git add .
git commit -m "Initial bot"
# Create a repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/mexc-bot.git
git push -u origin main
```

3. **In Railway:**
   - Click "New Project" → "Deploy from GitHub repo"
   - Select your repo
   - Go to **Variables** tab and add:
     ```
       MEXC_API_KEY             = your_key
       MEXC_API_SECRET          = your_secret
       ANTHROPIC_API_KEY        = your_key   # optional, only needed for Telegram /ask
       PAPER_TRADE              = true
       TRADE_BUDGET             = 50
       SCALPER_ALLOCATION_PCT   = 0.25
       MOONSHOT_ALLOCATION_PCT  = 0.45
       TRINITY_ALLOCATION_PCT   = 0.10
       GRID_ALLOCATION_PCT      = 0.20
       SCALPER_BUDGET_PCT       = 0.42
       MOONSHOT_BUDGET_PCT      = 0.048
       REVERSAL_BUDGET_PCT      = 0.12
       TRINITY_BUDGET_PCT       = 0.20
       GRID_BUDGET_PCT          = 0.40
       MAX_CONSECUTIVE_LOSSES   = 4
       STREAK_AUTO_RESET_MINS   = 60
       SESSION_LOSS_PAUSE_PCT   = 0.03
       SESSION_LOSS_PAUSE_MINS  = 120
        MEXCBOT_STATE_FILE = /data/runtime_state.json
     SCAN_INTERVAL     = 60
     ```
   - Railway will auto-deploy. Click **Logs** to watch it run.
      - Mount a Railway volume at `/data` before live cutover. The runtime state file must live on that volume or open positions and pause/cooldown guards are lost on restart.

4. **When you're confident** (after watching paper trades for a few days):
   - Change `PAPER_TRADE` to `false` in Railway Variables
   - Railway redeploys automatically

   See `RAILWAY_MIGRATION.md` for the monolith-to-modular variable mapping and the known semantic mismatches.

### Daily Backtest Refresh

Use a separate Railway cron service for the calibration refresh instead of tying it to the live bot service.

- Command:
   ```bash
   python -m backtest.run_daily_calibration
   ```
- Recommended schedule:
   - every day, 10 to 20 minutes after the candle boundary you care about
   - for the current setup, `15m` past the hour is a safe default because it gives the last `5m` and `15m` candles time to close and land cleanly
- Required environment:
   - same `REDIS_URL` as the live bot
   - same `MEXCBOT_CALIBRATION_REDIS_KEY`
   - same `MEXCBOT_DAILY_REVIEW_REDIS_KEY`
   - rolling-window vars such as `BACKTEST_ROLLING_DAYS`, `BACKTEST_INTERVAL`, and the strategy symbol universes
   - optional: `ANTHROPIC_API_KEY` for AI-written operator summaries

This keeps the workflow dynamic: the cron job reruns the rolling calibration, generates a last-24h review of the best and worst opportunity lanes, publishes both payloads, and the live bot keeps reloading them on its normal refresh cadence.

For fast manual exploration, use a shorter rolling window such as `BACKTEST_ROLLING_DAYS=3` or `7` with reduced strategy symbol lists. For the scheduled Railway job, keep the broader strategy universes and your normal rolling horizon.

---

## Deploy to Render (alternative)

1. Create account at [render.com](https://render.com)
2. New → **Background Worker** → Connect GitHub repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Add environment variables in the Render dashboard

---

## Getting Your MEXC API Key

1. Log into [mexc.com](https://mexc.com)
2. Profile → **API Management**
3. Create new API key
4. Enable **Spot Trading** permission only (do NOT enable withdrawals)
5. Whitelist your server IP for extra safety (Railway/Render give you a static IP on paid plans — on free tier, skip this)

---

## Reading the Logs

```
2024-01-15 10:32:01 [INFO] 🔍 Scanning market for opportunities...
2024-01-15 10:32:45 [INFO] 📊 Top pick: SOLUSDT | Score: 67.5 | RSI: 28.3 | Vol ratio: 2.4x | Price: 98.23
2024-01-15 10:32:45 [INFO] 📝 [PAPER] BUY 0.509 SOLUSDT @ MARKET
2024-01-15 10:32:45 [INFO] 🟢 Opened: SOLUSDT | Entry: 98.23 | TP: 100.19 | SL: 96.75
2024-01-15 10:33:00 [INFO] 👀 Holding SOLUSDT | +0.43% | Price: 98.65
2024-01-15 10:38:12 [INFO] 🎯 TP hit: SOLUSDT | +2.03% | Price: 100.22
2024-01-15 10:38:12 [INFO] 📈 Stats | Trades: 1 | Win rate: 100% | Total P&L: $+1.03
2024-01-15 10:38:17 [INFO] 🔄 Trade closed. Scanning for next opportunity immediately...
```

---

## Risk Warnings

- **Start with PAPER_TRADE=true** and observe for several days before going live
- The bot can hold multiple positions, but each allocation bucket still puts real capital at risk per trade
- Crypto markets can gap through stop losses in extreme volatility (flash crashes)
- Past paper performance does not guarantee live performance
- Never trade more than you can afford to lose entirely

---

## File Structure

```
mexc-bot/
├── bot.py                    # Runtime selector entrypoint
├── mexcbot/
│   ├── config.py             # Env parsing and runtime settings
│   ├── exchange.py           # MEXC REST client helpers
│   ├── indicators.py         # Shared EMA/RSI utilities
│   ├── models.py             # Opportunity and trade dataclasses
│   ├── exits.py              # Shared breakeven / trailing / flat-exit logic
│   ├── runtime.py            # Live runtime loop
│   └── strategies/
│       ├── scalper.py        # Extracted RSI/EMA/volume momentum path
│       ├── grid.py           # Extracted mean-reversion scanner
│       └── trinity.py        # Extracted deep-dip recovery scanner
├── backtest/
│   ├── config.py             # Backtest runtime settings
│   ├── data.py               # Historical MEXC kline downloader with local cache
│   ├── engine.py             # Multi-position backtest engine with fee/slippage/fill modeling
│   ├── reporter.py           # Summary metrics and artifact export
│   └── run_backtest.py       # CLI backtest entrypoint
├── tests/                    # Unit tests for indicators, strategy, and backtest
├── requirements.txt
├── .env.example
├── .gitignore
├── railway.toml
└── trades.log
```
