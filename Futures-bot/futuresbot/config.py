from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


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


def resolve_workspace_path(value: str) -> str:
    raw_path = Path(value)
    if raw_path.is_absolute():
        return str(raw_path)
    return str((WORKSPACE_ROOT / raw_path).resolve())


def parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_backtest_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    reference_now = now.astimezone(timezone.utc) if now is not None else utc_now()
    rolling_days = env_float("FUTURES_BACKTEST_ROLLING_DAYS", env_float("BACKTEST_ROLLING_DAYS", 60.0))
    end_raw = env_str("FUTURES_BACKTEST_END", env_str("BACKTEST_END", ""))
    if end_raw:
        end = parse_utc_datetime(end_raw)
    else:
        aligned = reference_now.replace(minute=0, second=0, microsecond=0)
        end = aligned if aligned < reference_now else aligned - timedelta(hours=1)
    start_raw = env_str("FUTURES_BACKTEST_START", env_str("BACKTEST_START", ""))
    if start_raw:
        start = parse_utc_datetime(start_raw)
    else:
        start = end - timedelta(days=rolling_days)
    if start >= end:
        raise ValueError("Futures backtest start must be earlier than end.")
    return start, end


@dataclass(slots=True)
class FuturesConfig:
    api_key: str
    api_secret: str
    telegram_token: str
    telegram_chat_id: str
    paper_trade: bool
    symbol: str
    futures_base_url: str
    margin_budget_usdt: float
    max_margin_fraction: float
    min_confidence_score: float
    hourly_check_seconds: int
    heartbeat_seconds: int
    calibration_file: str
    calibration_redis_key: str
    calibration_refresh_seconds: int
    calibration_max_age_hours: float
    calibration_min_total_trades: int
    review_file: str
    review_redis_key: str
    anthropic_api_key: str
    runtime_state_file: str
    status_file: str
    recv_window_seconds: int
    leverage_min: int
    leverage_max: int
    hard_loss_cap_pct: float
    adx_floor: float
    trend_24h_floor: float
    trend_6h_floor: float
    breakout_buffer_atr: float
    consolidation_window_bars: int
    consolidation_max_range_pct: float
    consolidation_atr_mult: float
    volume_ratio_floor: float
    tp_atr_mult: float
    tp_range_mult: float
    tp_floor_pct: float
    sl_buffer_atr_mult: float
    sl_trend_atr_mult: float
    min_reward_risk: float
    early_exit_tp_progress: float
    early_exit_min_profit_pct: float
    early_exit_buffer_pct: float
    open_type: int = 1
    position_mode: int = 2

    @classmethod
    def from_env(cls) -> "FuturesConfig":
        hourly_check_seconds = env_int("FUTURES_HOURLY_CHECK_SECONDS", 3600)
        return cls(
            api_key=env_str("MEXC_API_KEY", ""),
            api_secret=env_str("MEXC_API_SECRET", ""),
            telegram_token=env_str("FUTURES_TELEGRAM_TOKEN", env_str("TELEGRAM_TOKEN", "")),
            telegram_chat_id=env_str("FUTURES_TELEGRAM_CHAT_ID", env_str("TELEGRAM_CHAT_ID", "")),
            paper_trade=env_bool("FUTURES_PAPER_TRADE", True),
            symbol=env_str("FUTURES_SYMBOL", "BTC_USDT").upper(),
            futures_base_url=env_str("MEXC_FUTURES_BASE_URL", "https://api.mexc.com"),
            margin_budget_usdt=env_float("FUTURES_MARGIN_BUDGET_USDT", 75.0),
            max_margin_fraction=env_float("FUTURES_MAX_MARGIN_FRACTION", 0.85),
            min_confidence_score=env_float("FUTURES_SCORE_THRESHOLD", 56.0),
            hourly_check_seconds=hourly_check_seconds,
            heartbeat_seconds=env_int("FUTURES_HEARTBEAT_SECONDS", env_int("HEARTBEAT_SECONDS", hourly_check_seconds)),
            calibration_file=resolve_workspace_path(env_str("FUTURES_CALIBRATION_FILE", "Futures-bot/backtest_output/calibration.json")),
            calibration_redis_key=env_str("FUTURES_CALIBRATION_REDIS_KEY", "mexc_futures_calibration"),
            calibration_refresh_seconds=env_int("FUTURES_CALIBRATION_REFRESH_SECONDS", 900),
            calibration_max_age_hours=env_float("FUTURES_CALIBRATION_MAX_AGE_HOURS", 72.0),
            calibration_min_total_trades=env_int("FUTURES_CALIBRATION_MIN_TOTAL_TRADES", 4),
            review_file=resolve_workspace_path(env_str("FUTURES_DAILY_REVIEW_FILE", "Futures-bot/backtest_output/daily_review.json")),
            review_redis_key=env_str("FUTURES_DAILY_REVIEW_REDIS_KEY", "mexc_futures_daily_review"),
            anthropic_api_key=env_str("ANTHROPIC_API_KEY", ""),
            runtime_state_file=resolve_workspace_path(env_str("FUTURES_RUNTIME_STATE_FILE", "Futures-bot/futures_runtime_state.json")),
            status_file=resolve_workspace_path(env_str("FUTURES_STATUS_FILE", "Futures-bot/futures_runtime_status.json")),
            recv_window_seconds=env_int("FUTURES_RECV_WINDOW_SECONDS", 30),
            leverage_min=env_int("FUTURES_LEVERAGE_MIN", 20),
            leverage_max=env_int("FUTURES_LEVERAGE_MAX", 50),
            hard_loss_cap_pct=env_float("FUTURES_HARD_LOSS_CAP_PCT", 0.75),
            adx_floor=env_float("FUTURES_ADX_FLOOR", 18.0),
            trend_24h_floor=env_float("FUTURES_TREND_24H_FLOOR", 0.009),
            trend_6h_floor=env_float("FUTURES_TREND_6H_FLOOR", 0.003),
            breakout_buffer_atr=env_float("FUTURES_BREAKOUT_BUFFER_ATR", 0.18),
            consolidation_window_bars=env_int("FUTURES_CONSOLIDATION_WINDOW_BARS", 16),
            consolidation_max_range_pct=env_float("FUTURES_CONSOLIDATION_MAX_RANGE_PCT", 0.018),
            consolidation_atr_mult=env_float("FUTURES_CONSOLIDATION_ATR_MULT", 1.55),
            volume_ratio_floor=env_float("FUTURES_VOLUME_RATIO_FLOOR", 1.0),
            tp_atr_mult=env_float("FUTURES_TP_ATR_MULT", 5.8),
            tp_range_mult=env_float("FUTURES_TP_RANGE_MULT", 1.45),
            tp_floor_pct=env_float("FUTURES_TP_FLOOR_PCT", 0.022),
            sl_buffer_atr_mult=env_float("FUTURES_SL_BUFFER_ATR_MULT", 0.85),
            sl_trend_atr_mult=env_float("FUTURES_SL_TREND_ATR_MULT", 1.55),
            min_reward_risk=env_float("FUTURES_MIN_REWARD_RISK", 1.15),
            early_exit_tp_progress=env_float("FUTURES_EARLY_EXIT_TP_PROGRESS", 0.90),
            early_exit_min_profit_pct=env_float("FUTURES_EARLY_EXIT_MIN_PROFIT_PCT", 0.012),
            early_exit_buffer_pct=env_float("FUTURES_EARLY_EXIT_BUFFER_PCT", 0.10),
            open_type=env_int("FUTURES_OPEN_TYPE", 1),
            position_mode=env_int("FUTURES_POSITION_MODE", 2),
        )


@dataclass(slots=True)
class FuturesBacktestConfig:
    start: datetime
    end: datetime
    symbol: str
    initial_balance: float
    margin_budget_usdt: float
    taker_fee_rate: float
    calibration_file: str
    calibration_redis_key: str
    calibration_min_total_trades: int
    review_file: str
    review_redis_key: str
    output_dir: str
    cache_dir: str
    min_confidence_score: float
    leverage_min: int
    leverage_max: int
    hard_loss_cap_pct: float
    adx_floor: float
    trend_24h_floor: float
    trend_6h_floor: float
    breakout_buffer_atr: float
    consolidation_window_bars: int
    consolidation_max_range_pct: float
    consolidation_atr_mult: float
    volume_ratio_floor: float
    tp_atr_mult: float
    tp_range_mult: float
    tp_floor_pct: float
    sl_buffer_atr_mult: float
    sl_trend_atr_mult: float
    min_reward_risk: float
    early_exit_tp_progress: float
    early_exit_min_profit_pct: float
    early_exit_buffer_pct: float
    redis_url: str = ""
    anthropic_api_key: str = ""

    @classmethod
    def from_env(cls, now: datetime | None = None) -> "FuturesBacktestConfig":
        start, end = resolve_backtest_window(now=now)
        live = FuturesConfig.from_env()
        return cls(
            start=start,
            end=end,
            symbol=live.symbol,
            initial_balance=env_float("FUTURES_BACKTEST_INITIAL_BALANCE", 300.0),
            margin_budget_usdt=env_float("FUTURES_BACKTEST_MARGIN_BUDGET_USDT", live.margin_budget_usdt),
            taker_fee_rate=env_float("FUTURES_BACKTEST_TAKER_FEE_RATE", 0.0004),
            calibration_file=live.calibration_file,
            calibration_redis_key=live.calibration_redis_key,
            calibration_min_total_trades=live.calibration_min_total_trades,
            review_file=live.review_file,
            review_redis_key=live.review_redis_key,
            output_dir=resolve_workspace_path(env_str("FUTURES_BACKTEST_OUTPUT_DIR", "Futures-bot/backtest_output")),
            cache_dir=resolve_workspace_path(env_str("FUTURES_BACKTEST_CACHE_DIR", "Futures-bot/backtest_cache")),
            min_confidence_score=env_float("FUTURES_BACKTEST_SCORE_THRESHOLD", live.min_confidence_score),
            leverage_min=live.leverage_min,
            leverage_max=live.leverage_max,
            hard_loss_cap_pct=live.hard_loss_cap_pct,
            adx_floor=live.adx_floor,
            trend_24h_floor=live.trend_24h_floor,
            trend_6h_floor=live.trend_6h_floor,
            breakout_buffer_atr=live.breakout_buffer_atr,
            consolidation_window_bars=live.consolidation_window_bars,
            consolidation_max_range_pct=live.consolidation_max_range_pct,
            consolidation_atr_mult=live.consolidation_atr_mult,
            volume_ratio_floor=live.volume_ratio_floor,
            tp_atr_mult=live.tp_atr_mult,
            tp_range_mult=live.tp_range_mult,
            tp_floor_pct=live.tp_floor_pct,
            sl_buffer_atr_mult=live.sl_buffer_atr_mult,
            sl_trend_atr_mult=live.sl_trend_atr_mult,
            min_reward_risk=live.min_reward_risk,
            early_exit_tp_progress=live.early_exit_tp_progress,
            early_exit_min_profit_pct=live.early_exit_min_profit_pct,
            early_exit_buffer_pct=live.early_exit_buffer_pct,
            redis_url=env_str("REDIS_URL", ""),
            anthropic_api_key=live.anthropic_api_key,
        )