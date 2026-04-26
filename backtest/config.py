from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re

from mexcbot.config import env_bool, env_csv, env_float, env_int, env_str


def parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def interval_to_timedelta(interval: str) -> timedelta:
    match = re.fullmatch(r"(?P<value>\d+)(?P<unit>[mhd])", interval.strip().lower())
    if match is None:
        raise ValueError(f"Unsupported backtest interval: {interval}")

    value = int(match.group("value"))
    unit = match.group("unit")
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


def align_to_closed_candle(moment: datetime, interval: str) -> datetime:
    step = interval_to_timedelta(interval)
    epoch_seconds = int(moment.astimezone(timezone.utc).timestamp())
    step_seconds = int(step.total_seconds())
    return datetime.fromtimestamp((epoch_seconds // step_seconds) * step_seconds, tz=timezone.utc)


def resolve_backtest_window(interval: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    reference_now = now.astimezone(timezone.utc) if now is not None else utc_now()
    rolling_days = env_float("BACKTEST_ROLLING_DAYS", 31.0)
    end_offset_hours = env_float("BACKTEST_END_OFFSET_HOURS", 0.0)

    end_raw = env_str("BACKTEST_END", "")
    if end_raw:
        end = parse_utc_datetime(end_raw)
    else:
        end = align_to_closed_candle(reference_now - timedelta(hours=end_offset_hours), interval)

    start_raw = env_str("BACKTEST_START", "")
    if start_raw:
        start = parse_utc_datetime(start_raw)
    else:
        start = end - timedelta(days=rolling_days)

    if start >= end:
        raise ValueError("Backtest start must be earlier than end.")

    return start, end


@dataclass(slots=True)
class BacktestConfig:
    start: datetime
    end: datetime
    symbols: list[str]
    strategies: list[str] = field(default_factory=lambda: ["SCALPER", "GRID", "TRINITY", "MOONSHOT", "REVERSAL"])
    scalper_symbols: list[str] = field(default_factory=list)
    grid_symbols: list[str] = field(default_factory=list)
    trinity_symbols: list[str] = field(default_factory=list)
    moonshot_symbols: list[str] = field(default_factory=list)
    reversal_symbols: list[str] = field(default_factory=list)
    interval: str = "5m"
    initial_balance: float = 500.0
    trade_budget: float = 50.0
    max_open_positions: int = 3
    reentry_cooldown_bars: int = 12
    timeout_cooldown_bars: int = 288  # 24h at 5-min bars; extended cooldown after TIMEOUT exits
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.001
    maker_slippage_rate: float = 0.0002
    taker_slippage_rate: float = 0.001
    maker_fill_ratio: float = 1.0
    taker_fill_ratio: float = 1.0
    synthetic_defensive_unlock_bars: int = 0
    synthetic_close_max_attempts: int = 1
    synthetic_retry_delay_bars: int = 1
    synthetic_dust_threshold_usdt: float = 3.0
    synthetic_close_verify_ratio: float = 0.01
    synthetic_dust_sweep_enabled: bool = False
    synthetic_dust_conversion_fee_rate: float = 0.0
    take_profit_pct: float = 0.020
    stop_loss_pct: float = 0.40
    score_threshold: float = 37.0
    scalper_threshold: float = 42.0
    moonshot_min_score: float = 32.0
    adaptive_window: int = 16
    adaptive_decay_rate: float = 0.15
    adaptive_tighten_step: float = 3.0
    adaptive_relax_step: float = 2.0
    adaptive_max_offset: float = 10.0
    adaptive_min_offset: float = -6.0
    scalper_allocation_pct: float = 0.25
    moonshot_allocation_pct: float = 0.45
    trinity_allocation_pct: float = 0.10
    grid_allocation_pct: float = 0.20
    scalper_budget_pct: float = 0.37
    moonshot_budget_pct: float = 0.048
    trinity_budget_pct: float = 0.20
    grid_budget_pct: float = 0.40
    perf_rebalance_trades: int = 20
    perf_scalper_floor: float = 0.10
    perf_scalper_ceil: float = 0.40
    perf_moonshot_floor: float = 0.02
    perf_moonshot_ceil: float = 0.14
    perf_shift_step: float = 0.028
    regime_high_vol_atr_ratio: float = 1.85
    regime_low_vol_atr_ratio: float = 0.80
    regime_strong_uptrend_gap: float = 0.02
    regime_strong_downtrend_gap: float = -0.02
    regime_tighten_mult: float = 1.15
    regime_loosen_mult: float = 0.92
    regime_trend_mult: float = 0.92
    output_dir: str = "backtest_output"
    cache_dir: str = "backtest_cache"
    calibration_file: str = "backtest_output/calibration.json"
    redis_url: str = ""
    calibration_redis_key: str = "mexc_trade_calibration"
    calibration_min_strategy_trades: int = 12
    calibration_min_symbol_trades: int = 8
    moonshot_btc_ema_gate: float = -0.02
    moonshot_btc_gate_reopen: float = -0.01
    fear_greed_bear_threshold: int = 15
    fear_greed_extreme_fear_threshold: int = 20
    fear_greed_extreme_fear_mult: float = 1.40
    fear_greed_bear_block_moonshot: bool = True

    def symbols_for_strategy(self, strategy: str) -> list[str]:
        resolved = strategy.upper()
        if resolved == "SCALPER":
            return self.scalper_symbols or self.symbols
        if resolved == "GRID":
            return self.grid_symbols or self.symbols
        if resolved == "TRINITY":
            return self.trinity_symbols or self.symbols
        if resolved == "MOONSHOT":
            return self.moonshot_symbols or self.symbols
        if resolved == "REVERSAL":
            return self.reversal_symbols or self.symbols
        return self.symbols

    @classmethod
    def from_env(cls, now: datetime | None = None) -> "BacktestConfig":
        interval = env_str("BACKTEST_INTERVAL", "5m")
        start, end = resolve_backtest_window(interval=interval, now=now)
        return cls(
            start=start,
            end=end,
            symbols=env_csv("BACKTEST_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"),
            strategies=env_csv("MEXCBOT_STRATEGIES", "SCALPER,GRID,TRINITY,MOONSHOT,REVERSAL"),
            scalper_symbols=env_csv(
                "BACKTEST_SCALPER_SYMBOLS",
                "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT",
            ),
            grid_symbols=env_csv("BACKTEST_GRID_SYMBOLS", env_str("GRID_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ADAUSDT")),
            trinity_symbols=env_csv("BACKTEST_TRINITY_SYMBOLS", env_str("TRINITY_SYMBOLS", "BTCUSDT,SOLUSDT,ETHUSDT")),
            moonshot_symbols=env_csv(
                "BACKTEST_MOONSHOT_SYMBOLS",
                env_str("MOONSHOT_SYMBOLS", "SOLUSDT,DOGEUSDT,PEPEUSDT,ENAUSDT,WIFUSDT,BONKUSDT"),
            ),
            reversal_symbols=env_csv(
                "BACKTEST_REVERSAL_SYMBOLS",
                env_str("REVERSAL_SYMBOLS", "SOLUSDT,DOGEUSDT,ETHUSDT,PEPEUSDT,WIFUSDT"),
            ),
            interval=interval,
            initial_balance=env_float("BACKTEST_INITIAL_BALANCE", 500.0),
            trade_budget=env_float("BACKTEST_TRADE_BUDGET", env_float("TRADE_BUDGET", 50.0)),
            max_open_positions=env_int("BACKTEST_MAX_OPEN_POSITIONS", env_int("MAX_OPEN_POSITIONS", 3)),
            reentry_cooldown_bars=env_int("BACKTEST_REENTRY_COOLDOWN_BARS", 12),
            timeout_cooldown_bars=env_int("BACKTEST_TIMEOUT_COOLDOWN_BARS", 288),
            maker_fee_rate=env_float("BACKTEST_MAKER_FEE_RATE", 0.0),
            taker_fee_rate=env_float("BACKTEST_TAKER_FEE_RATE", 0.001),
            maker_slippage_rate=env_float("BACKTEST_MAKER_SLIPPAGE_RATE", 0.0002),
            taker_slippage_rate=env_float("BACKTEST_TAKER_SLIPPAGE_RATE", 0.001),
            maker_fill_ratio=env_float("BACKTEST_MAKER_FILL_RATIO", 1.0),
            taker_fill_ratio=env_float("BACKTEST_TAKER_FILL_RATIO", 1.0),
            synthetic_defensive_unlock_bars=env_int("BACKTEST_SYNTHETIC_DEFENSIVE_UNLOCK_BARS", 0),
            synthetic_close_max_attempts=env_int("BACKTEST_SYNTHETIC_CLOSE_MAX_ATTEMPTS", 1),
            synthetic_retry_delay_bars=env_int("BACKTEST_SYNTHETIC_RETRY_DELAY_BARS", 1),
            synthetic_dust_threshold_usdt=env_float("BACKTEST_SYNTHETIC_DUST_THRESHOLD_USDT", env_float("DUST_THRESHOLD", 3.0)),
            synthetic_close_verify_ratio=env_float("BACKTEST_SYNTHETIC_CLOSE_VERIFY_RATIO", env_float("CLOSE_VERIFY_RATIO", 0.01)),
            synthetic_dust_sweep_enabled=env_bool("BACKTEST_SYNTHETIC_DUST_SWEEP_ENABLED", False),
            synthetic_dust_conversion_fee_rate=env_float("BACKTEST_SYNTHETIC_DUST_CONVERSION_FEE_RATE", 0.0),
            take_profit_pct=env_float("BACKTEST_TAKE_PROFIT_PCT", env_float("TAKE_PROFIT_PCT", 0.020)),
            stop_loss_pct=env_float("BACKTEST_STOP_LOSS_PCT", env_float("STOP_LOSS_PCT", 0.40)),
            score_threshold=env_float("BACKTEST_SCORE_THRESHOLD", env_float("SCORE_THRESHOLD", 37.0)),
            scalper_threshold=env_float("BACKTEST_SCALPER_THRESHOLD", env_float("SCALPER_THRESHOLD", env_float("SCORE_THRESHOLD", 42.0))),
            moonshot_min_score=env_float("BACKTEST_MOONSHOT_MIN_SCORE", env_float("MOONSHOT_MIN_SCORE", 32.0)),
            adaptive_window=env_int("ADAPTIVE_WINDOW", 16),
            adaptive_decay_rate=env_float("ADAPTIVE_DECAY_RATE", 0.15),
            adaptive_tighten_step=env_float("ADAPTIVE_TIGHTEN_STEP", 3.0),
            adaptive_relax_step=env_float("ADAPTIVE_RELAX_STEP", 2.0),
            adaptive_max_offset=env_float("ADAPTIVE_MAX_OFFSET", 10.0),
            adaptive_min_offset=env_float("ADAPTIVE_MIN_OFFSET", -6.0),
            scalper_allocation_pct=env_float("SCALPER_ALLOCATION_PCT", 0.25),
            moonshot_allocation_pct=env_float("MOONSHOT_ALLOCATION_PCT", 0.45),
            trinity_allocation_pct=env_float("TRINITY_ALLOCATION_PCT", 0.10),
            grid_allocation_pct=env_float("GRID_ALLOCATION_PCT", 0.20),
            scalper_budget_pct=env_float("SCALPER_BUDGET_PCT", 0.37),
            moonshot_budget_pct=env_float("MOONSHOT_BUDGET_PCT", 0.048),
            trinity_budget_pct=env_float("TRINITY_BUDGET_PCT", 0.20),
            grid_budget_pct=env_float("GRID_BUDGET_PCT", 0.40),
            perf_rebalance_trades=env_int("PERF_REBALANCE_TRADES", 20),
            perf_scalper_floor=env_float("PERF_SCALPER_FLOOR", 0.10),
            perf_scalper_ceil=env_float("PERF_SCALPER_CEIL", 0.40),
            perf_moonshot_floor=env_float("PERF_MOONSHOT_FLOOR", 0.02),
            perf_moonshot_ceil=env_float("PERF_MOONSHOT_CEIL", 0.14),
            perf_shift_step=env_float("PERF_SHIFT_STEP", 0.028),
            regime_high_vol_atr_ratio=env_float("REGIME_HIGH_VOL_ATR_RATIO", 1.85),
            regime_low_vol_atr_ratio=env_float("REGIME_LOW_VOL_ATR_RATIO", 0.80),
            regime_strong_uptrend_gap=env_float("REGIME_STRONG_UPTREND_GAP", 0.02),
            regime_strong_downtrend_gap=env_float("REGIME_STRONG_DOWNTREND_GAP", -0.02),
            regime_tighten_mult=env_float("REGIME_TIGHTEN_MULT", 1.15),
            regime_loosen_mult=env_float("REGIME_LOOSEN_MULT", 0.92),
            regime_trend_mult=env_float("REGIME_TREND_MULT", 0.92),
            output_dir=env_str("BACKTEST_OUTPUT_DIR", "backtest_output"),
            cache_dir=env_str("BACKTEST_CACHE_DIR", "backtest_cache"),
            calibration_file=env_str("MEXCBOT_CALIBRATION_FILE", "backtest_output/calibration.json"),
            redis_url=env_str("REDIS_URL", ""),
            calibration_redis_key=env_str("MEXCBOT_CALIBRATION_REDIS_KEY", "mexc_trade_calibration"),
            calibration_min_strategy_trades=env_int("MEXCBOT_CALIBRATION_MIN_STRATEGY_TRADES", 12),
            calibration_min_symbol_trades=env_int("MEXCBOT_CALIBRATION_MIN_SYMBOL_TRADES", 8),
            moonshot_btc_ema_gate=env_float("MOONSHOT_BTC_EMA_GATE", -0.02),
            moonshot_btc_gate_reopen=env_float("MOONSHOT_BTC_GATE_REOPEN", -0.01),
            fear_greed_bear_threshold=env_int("FG_BEAR_THRESHOLD", 15),
            fear_greed_extreme_fear_threshold=env_int("FG_EXTREME_FEAR_THRESHOLD", 20),
            fear_greed_extreme_fear_mult=env_float("FG_EXTREME_FEAR_MULT", 1.40),
            fear_greed_bear_block_moonshot=env_bool("FG_BEAR_BLOCK_MOONSHOT", True),
        )