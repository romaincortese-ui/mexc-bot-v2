# Railway Migration Sheet

This project is not a drop-in env-for-env replacement for the monolith. Use this sheet when cutting Railway over to the modular runtime.

## Required before live cutover

1. Mount a persistent Railway volume, for example at `/data`.
2. Set `MEXCBOT_STATE_FILE=/data/runtime_state.json`.
3. Keep `PAPER_TRADE=true` through at least one restart or redeploy test.
4. Verify `MEXCBOT_STRATEGIES` intentionally matches the modular strategy set you want to run.

Without a volume-backed state file, the bot will lose open position tracking, cooldowns, and pause guards on restart.

## Use as-is

- `MEXC_API_KEY`
- `MEXC_API_SECRET`
- `PAPER_TRADE`
- `TRADE_BUDGET`
- `MAX_OPEN_POSITIONS`
- `SCAN_INTERVAL`
- `PRICE_CHECK_INTERVAL`
- `TAKE_PROFIT_PCT`
- `STOP_LOSS_PCT`
- `MIN_VOLUME_USDT`
- `MIN_ABS_CHANGE_PCT`
- `SCORE_THRESHOLD`
- `SCALPER_THRESHOLD`
- `MOONSHOT_MIN_SCORE`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `REDIS_URL`
- `MEXCBOT_CALIBRATION_REDIS_KEY`
- `MEXCBOT_CALIBRATION_FILE`
- `MEXCBOT_CALIBRATION_REFRESH_SECONDS`
- `MEXCBOT_CALIBRATION_MAX_AGE_HOURS`
- `MEXCBOT_CALIBRATION_MIN_TOTAL_TRADES`
- `HEARTBEAT_SECONDS`

## New or now required in the modular bot

- `MEXCBOT_STATE_FILE`: path to the persisted runtime state file. On Railway this should point to a mounted volume, for example `/data/runtime_state.json`.
- `MEXCBOT_STRATEGIES`: explicit strategy allowlist for the modular runtime.
- `MOONSHOT_SYMBOLS`, `REVERSAL_SYMBOLS`, `GRID_SYMBOLS`, `TRINITY_SYMBOLS`: modular symbol universes are driven directly by env.
- `MOONSHOT_TRENDING_SOURCE`: automatic moonshot trending source. `coingecko` enables CoinGecko's public trending feed, `off` disables auto-fetching.
- `MOONSHOT_TRENDING_SYMBOLS`: optional manually supplied trending universe for moonshot scoring.
- `MOONSHOT_TRENDING_BOOST`: score bonus applied to symbols present in `MOONSHOT_TRENDING_SYMBOLS`.
- `MOONSHOT_TRENDING_CACHE_MINS`, `MOONSHOT_TRENDING_MAX_COINS`: controls for the automatic trending source.
- `WEB_SEARCH_ENABLED`: enables Anthropic web-search-backed moonshot buzz scoring.
- `MOONSHOT_SOCIAL_BOOST_MAX`, `MOONSHOT_SOCIAL_CACHE_MINS`: controls for cached social buzz scoring.
- `MOONSHOT_SOCIAL_MAX_EVALS`: maximum number of near-threshold moonshot candidates that will trigger a buzz lookup in one scan.

## Same name, different meaning

- `MAX_OPEN_POSITIONS`: modular runtime enforces a global cap, while some monolith behavior was effectively strategy-specific.
- `TRINITY_SYMBOLS` and the default Trinity configuration: defaults differ from the monolith, so keep them explicit.

## Same name, aligned meaning

- `MOONSHOT_MAX_VOL_RATIO`: now behaves like the monolith again. It scales the moonshot maximum eligible 24h quote volume by account size, with a protective floor for small balances.

## Present in modular code, but not parity-complete

- Moonshot now has automatic CoinGecko trending discovery, manual override symbols, and Anthropic web-search buzz scoring for near-threshold candidates. The modular bot intentionally improves on the monolith by limiting buzz lookups per scan and scaling buzz by technical quality instead of adding raw hype points. It still does not replicate the monolith's exact prompts or sentiment stack one-for-one.
- Runtime still uses REST polling rather than the monolith's websocket-style live monitoring path.

## Monolith-only or unsupported for direct carry-over

These appear in sweep and audit tooling as monolith-only or behaviorally different. Review each one instead of migrating it mechanically.

- `CHASE_LIMIT_TIMEOUT`
- `DUST_THRESHOLD`
- `MOONSHOT_MIN_NOTIONAL`
- `SCALPER_BREAKEVEN_SCORE`
- `SCALPER_EMA50_PENALTY`
- `SCALPER_MIN_1H_VOL`
- `SCALPER_MIN_ATR_PCT`
- `SCALPER_PARTIAL_TP_SCORE`
- `SCALPER_RISK_PER_TRADE`
- `SCALPER_ROTATE_GAP`
- `SCALPER_SL_MULT_DEFAULT`
- `MICRO_TP_MIN_PROFIT`
- `FEE_SLIPPAGE_BUFFER`
- `MOMENTUM_DECAY_CANDLES`
- `GIVEBACK_TARGET_HIGH`
- `PROG_TRAIL_CEILING`
- `PROG_TRAIL_FLOOR`
- `PROG_TRAIL_TIGHTEN`
- `PROG_TRAIL_VOL_ANCHOR`
- `PROG_TRAIL_VOL_MIN`
- `PROG_TRAIL_VOL_MAX`
- `SCALPER_PROG_CEILING`
- `SCALPER_PROG_FLOOR`
- `SCALPER_PROG_TIGHTEN`
- `TRINITY_MAX_CONCURRENT`
- `SCALPER_STOP_CONFIRM_SECS`
- `GRID_STOP_CONFIRM_SECS`
- `TRINITY_STOP_CONFIRM_SECS`
- `REVERSAL_STOP_CONFIRM_SECS`
- `MOONSHOT_STOP_CONFIRM_SECS`
- `PREBREAKOUT_STOP_CONFIRM_SECS`

## Recommended Railway rollout

1. Deploy with `PAPER_TRADE=true` and the state file on a Railway volume.
2. Open at least one paper position, then force a restart and confirm the position is restored from state.
3. Confirm Telegram `/status` and `/reconcile` match the exchange after restart.
4. Only then flip `PAPER_TRADE=false`.