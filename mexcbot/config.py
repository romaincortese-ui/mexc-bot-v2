from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def env_csv(name: str, default: str) -> list[str]:
    raw = env_str(name, default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class LiveConfig:
    api_key: str
    api_secret: str
    paper_trade: bool
    trade_budget: float
    take_profit_pct: float
    stop_loss_pct: float
    scan_interval: int
    price_check_interval: int
    min_volume_usdt: float
    min_abs_change_pct: float
    universe_limit: int
    candidate_limit: int
    score_threshold: float
    scalper_threshold: float
    moonshot_min_score: float
    max_open_positions: int
    strategies: list[str]
    moonshot_symbols: list[str]
    reversal_symbols: list[str]
    grid_symbols: list[str]
    trinity_symbols: list[str]
    redis_url: str
    calibration_redis_key: str
    calibration_file: str
    calibration_refresh_seconds: int
    calibration_max_age_hours: float
    calibration_min_total_trades: int
    daily_review_redis_key: str
    daily_review_file: str
    daily_review_refresh_seconds: int
    daily_review_max_age_hours: float
    daily_review_min_total_trades: int
    daily_review_notify: bool
    anthropic_api_key: str
    telegram_token: str
    telegram_chat_id: str
    heartbeat_seconds: int
    scalper_symbol_cooldown_seconds: int
    scalper_rotation_cooldown_seconds: int
    max_consecutive_losses: int
    streak_auto_reset_mins: int
    win_rate_cb_window: int
    win_rate_cb_threshold: float
    win_rate_cb_pause_mins: int
    session_loss_pause_pct: float
    session_loss_pause_mins: int
    strategy_loss_streak_max: int
    strategy_loss_streak_mins: int
    moonshot_btc_ema_gate: float
    moonshot_btc_gate_reopen: float
    adaptive_window: int
    adaptive_decay_rate: float
    adaptive_tighten_step: float
    adaptive_relax_step: float
    adaptive_max_offset: float
    adaptive_min_offset: float
    scalper_allocation_pct: float
    moonshot_allocation_pct: float
    trinity_allocation_pct: float
    grid_allocation_pct: float
    scalper_budget_pct: float
    moonshot_budget_pct: float
    trinity_budget_pct: float
    grid_budget_pct: float
    perf_rebalance_trades: int
    perf_scalper_floor: float
    perf_scalper_ceil: float
    perf_moonshot_floor: float
    perf_moonshot_ceil: float
    perf_shift_step: float
    dead_coin_vol_scalper: float
    dead_coin_vol_moonshot: float
    dead_coin_spread_max: float
    dead_coin_consecutive: int
    dead_coin_blacklist_hours: int
    regime_high_vol_atr_ratio: float
    regime_low_vol_atr_ratio: float
    regime_strong_uptrend_gap: float
    regime_strong_downtrend_gap: float
    regime_tighten_mult: float
    regime_loosen_mult: float
    regime_trend_mult: float
    fear_greed_bear_threshold: int
    fear_greed_extreme_fear_threshold: int
    fear_greed_extreme_fear_mult: float
    fear_greed_bear_block_moonshot: bool
    fear_greed_bear_block_grid: bool
    grid_btc_1h_floor: float
    grid_btc_24h_floor: float
    state_file: str
    base_url: str = "https://api.mexc.com"

    @classmethod
    def from_env(cls) -> "LiveConfig":
        return cls(
            api_key=env_str("MEXC_API_KEY", ""),
            api_secret=env_str("MEXC_API_SECRET", ""),
            paper_trade=env_bool("PAPER_TRADE", True),
            trade_budget=env_float("TRADE_BUDGET", 50.0),
            take_profit_pct=env_float("TAKE_PROFIT_PCT", 0.020),
            stop_loss_pct=env_float("STOP_LOSS_PCT", 0.20),
            scan_interval=env_int("SCAN_INTERVAL", 60),
            price_check_interval=env_int("PRICE_CHECK_INTERVAL", 15),
            min_volume_usdt=env_float("MIN_VOLUME_USDT", 500_000.0),
            min_abs_change_pct=env_float("MIN_ABS_CHANGE_PCT", 0.5),
            universe_limit=env_int("UNIVERSE_LIMIT", 80),
            candidate_limit=env_int("CANDIDATE_LIMIT", 40),
            score_threshold=env_float("SCORE_THRESHOLD", 37.0),
            scalper_threshold=env_float("SCALPER_THRESHOLD", env_float("SCORE_THRESHOLD", 42.0)),
            moonshot_min_score=env_float("MOONSHOT_MIN_SCORE", 32.0),
            max_open_positions=env_int("MAX_OPEN_POSITIONS", 3),
            strategies=env_csv("MEXCBOT_STRATEGIES", "SCALPER,GRID,TRINITY,MOONSHOT,REVERSAL"),
            moonshot_symbols=env_csv("MOONSHOT_SYMBOLS", "SOLUSDT,DOGEUSDT,PEPEUSDT,ENAUSDT,WIFUSDT"),
            reversal_symbols=env_csv("REVERSAL_SYMBOLS", "SOLUSDT,DOGEUSDT,ETHUSDT,PEPEUSDT,WIFUSDT"),
            grid_symbols=env_csv("GRID_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ADAUSDT"),
            trinity_symbols=env_csv("TRINITY_SYMBOLS", "BTCUSDT,SOLUSDT,ETHUSDT"),
            redis_url=env_str("REDIS_URL", ""),
            calibration_redis_key=env_str("MEXCBOT_CALIBRATION_REDIS_KEY", "mexc_trade_calibration"),
            calibration_file=env_str("MEXCBOT_CALIBRATION_FILE", "backtest_output/calibration.json"),
            calibration_refresh_seconds=env_int("MEXCBOT_CALIBRATION_REFRESH_SECONDS", 300),
            calibration_max_age_hours=env_float("MEXCBOT_CALIBRATION_MAX_AGE_HOURS", 72.0),
            calibration_min_total_trades=env_int("MEXCBOT_CALIBRATION_MIN_TOTAL_TRADES", 50),
            daily_review_redis_key=env_str("MEXCBOT_DAILY_REVIEW_REDIS_KEY", "mexc_daily_review"),
            daily_review_file=env_str("MEXCBOT_DAILY_REVIEW_FILE", "backtest_output/daily_review.json"),
            daily_review_refresh_seconds=env_int("MEXCBOT_DAILY_REVIEW_REFRESH_SECONDS", 900),
            daily_review_max_age_hours=env_float("MEXCBOT_DAILY_REVIEW_MAX_AGE_HOURS", 36.0),
            daily_review_min_total_trades=env_int("MEXCBOT_DAILY_REVIEW_MIN_TOTAL_TRADES", 3),
            daily_review_notify=env_bool("MEXCBOT_DAILY_REVIEW_NOTIFY", True),
            anthropic_api_key=env_str("ANTHROPIC_API_KEY", ""),
            telegram_token=env_str("TELEGRAM_TOKEN", ""),
            telegram_chat_id=env_str("TELEGRAM_CHAT_ID", ""),
            heartbeat_seconds=env_int("HEARTBEAT_SECONDS", 3600),
            scalper_symbol_cooldown_seconds=env_int("SCALPER_SYMBOL_COOLDOWN", 1200),
            scalper_rotation_cooldown_seconds=env_int("SCALPER_ROTATION_SYMBOL_COOLDOWN", 900),
            max_consecutive_losses=env_int("MAX_CONSECUTIVE_LOSSES", 3),
            streak_auto_reset_mins=env_int("STREAK_AUTO_RESET_MINS", 120),
            win_rate_cb_window=env_int("WIN_RATE_CB_WINDOW", 10),
            win_rate_cb_threshold=env_float("WIN_RATE_CB_THRESHOLD", 0.30),
            win_rate_cb_pause_mins=env_int("WIN_RATE_CB_PAUSE_MINS", 60),
            session_loss_pause_pct=env_float("SESSION_LOSS_PAUSE_PCT", 0.03),
            session_loss_pause_mins=env_int("SESSION_LOSS_PAUSE_MINS", 120),
            strategy_loss_streak_max=env_int("STRATEGY_LOSS_STREAK_MAX", 3),
            strategy_loss_streak_mins=env_int("STRATEGY_LOSS_STREAK_MINS", 240),
            moonshot_btc_ema_gate=env_float("MOONSHOT_BTC_EMA_GATE", -0.02),
            moonshot_btc_gate_reopen=env_float("MOONSHOT_BTC_GATE_REOPEN", -0.01),
            adaptive_window=env_int("ADAPTIVE_WINDOW", 16),
            adaptive_decay_rate=env_float("ADAPTIVE_DECAY_RATE", 0.15),
            adaptive_tighten_step=env_float("ADAPTIVE_TIGHTEN_STEP", 3.0),
            adaptive_relax_step=env_float("ADAPTIVE_RELAX_STEP", 2.0),
            adaptive_max_offset=env_float("ADAPTIVE_MAX_OFFSET", 10.0),
            adaptive_min_offset=env_float("ADAPTIVE_MIN_OFFSET", -6.0),
            scalper_allocation_pct=env_float("SCALPER_ALLOCATION_PCT", 0.25),
            moonshot_allocation_pct=env_float("MOONSHOT_ALLOCATION_PCT", 0.45),
            trinity_allocation_pct=env_float("TRINITY_ALLOCATION_PCT", 0.20),
            grid_allocation_pct=env_float("GRID_ALLOCATION_PCT", 0.10),
            scalper_budget_pct=env_float("SCALPER_BUDGET_PCT", 0.37),
            moonshot_budget_pct=env_float("MOONSHOT_BUDGET_PCT", 0.048),
            trinity_budget_pct=env_float("TRINITY_BUDGET_PCT", 0.20),
            grid_budget_pct=env_float("GRID_BUDGET_PCT", 0.30),
            perf_rebalance_trades=env_int("PERF_REBALANCE_TRADES", 20),
            perf_scalper_floor=env_float("PERF_SCALPER_FLOOR", 0.10),
            perf_scalper_ceil=env_float("PERF_SCALPER_CEIL", 0.40),
            perf_moonshot_floor=env_float("PERF_MOONSHOT_FLOOR", 0.02),
            perf_moonshot_ceil=env_float("PERF_MOONSHOT_CEIL", 0.14),
            perf_shift_step=env_float("PERF_SHIFT_STEP", 0.028),
            dead_coin_vol_scalper=env_float("DEAD_COIN_VOL_SCALPER", 500000.0),
            dead_coin_vol_moonshot=env_float("DEAD_COIN_VOL_MOONSHOT", 150000.0),
            dead_coin_spread_max=env_float("DEAD_COIN_SPREAD_MAX", 0.003),
            dead_coin_consecutive=env_int("DEAD_COIN_CONSECUTIVE", 3),
            dead_coin_blacklist_hours=env_int("DEAD_COIN_BLACKLIST_HOURS", 24),
            regime_high_vol_atr_ratio=env_float("REGIME_HIGH_VOL_ATR_RATIO", 1.85),
            regime_low_vol_atr_ratio=env_float("REGIME_LOW_VOL_ATR_RATIO", 0.80),
            regime_strong_uptrend_gap=env_float("REGIME_STRONG_UPTREND_GAP", 0.02),
            regime_strong_downtrend_gap=env_float("REGIME_STRONG_DOWNTREND_GAP", -0.02),
            regime_tighten_mult=env_float("REGIME_TIGHTEN_MULT", 1.15),
            regime_loosen_mult=env_float("REGIME_LOOSEN_MULT", 0.92),
            regime_trend_mult=env_float("REGIME_TREND_MULT", 0.92),
            fear_greed_bear_threshold=env_int("FG_BEAR_THRESHOLD", 15),
            fear_greed_extreme_fear_threshold=env_int("FG_EXTREME_FEAR_THRESHOLD", 20),
            fear_greed_extreme_fear_mult=env_float("FG_EXTREME_FEAR_MULT", 1.40),
            fear_greed_bear_block_moonshot=env_bool("FG_BEAR_BLOCK_MOONSHOT", True),
            fear_greed_bear_block_grid=env_bool("FG_BEAR_BLOCK_GRID", True),
            grid_btc_1h_floor=env_float("GRID_BTC_1H_FLOOR", -0.005),
            grid_btc_24h_floor=env_float("GRID_BTC_24H_FLOOR", -0.015),
            state_file=env_str("MEXCBOT_STATE_FILE", "runtime_state.json"),
            base_url=env_str("MEXC_BASE_URL", "https://api.mexc.com"),
        )