from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


REPO_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_SYMBOL_PARAMETER_PROFILES: dict[str, dict[str, float | int]] = {
    # BTC remains the reference profile; values intentionally match the global defaults.
    "BTC_USDT": {},
    "ETH_USDT": {
        "trend_24h_floor": 0.008,
        "trend_6h_floor": 0.0025,
        "consolidation_max_range_pct": 0.020,
        "adx_floor": 17.0,
    },
    "SOL_USDT": {
        "trend_24h_floor": 0.010,
        "trend_6h_floor": 0.003,
        "consolidation_max_range_pct": 0.025,
        "adx_floor": 17.0,
    },
    "BNB_USDT": {
        "trend_24h_floor": 0.007,
        "trend_6h_floor": 0.002,
        "consolidation_max_range_pct": 0.016,
        "adx_floor": 17.0,
    },
    "PEPE_USDT": {
        "min_confidence_score": 60.0,
        "trend_24h_floor": 0.018,
        "trend_6h_floor": 0.0045,
        "consolidation_max_range_pct": 0.045,
        "consolidation_atr_mult": 2.20,
        "volume_ratio_floor": 1.10,
        "leverage_max": 25,
        "min_reward_risk": 1.25,
        "funding_rate_abs_max": 0.00025,
    },
    "TAO_USDT": {
        "min_confidence_score": 57.0,
        "trend_24h_floor": 0.014,
        "trend_6h_floor": 0.002,
        "consolidation_max_range_pct": 0.040,
        "consolidation_atr_mult": 2.00,
        "volume_ratio_floor": 1.05,
        "leverage_max": 25,
        "min_reward_risk": 1.20,
        "funding_rate_abs_max": 0.00022,
    },
}


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


def _symbol_env_prefix(symbol: str) -> str:
    """Normalize a symbol for use in per-symbol env var names.

    Strips non-alphanumerics so that e.g. ``XAUT_USDT`` -> ``FUTURES_XAUTUSDT_...``.
    This keeps env var names unambiguous (no consecutive underscores).
    """

    cleaned = "".join(ch for ch in symbol.upper() if ch.isalnum())
    return f"FUTURES_{cleaned}"


def env_str_for_symbol(symbol: str, suffix: str, default: str) -> str:
    value = os.getenv(f"{_symbol_env_prefix(symbol)}_{suffix}")
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_float_for_symbol(symbol: str, suffix: str, default: float) -> float:
    value = os.getenv(f"{_symbol_env_prefix(symbol)}_{suffix}")
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int_for_symbol(symbol: str, suffix: str, default: int) -> int:
    value = os.getenv(f"{_symbol_env_prefix(symbol)}_{suffix}")
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_symbol_list(raw: str, fallback: str) -> tuple[str, ...]:
    """Parse a comma-separated symbol list, preserving order and deduplicating."""

    if not raw.strip():
        return (fallback.upper(),)
    seen: list[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return tuple(seen) if seen else (fallback.upper(),)


def parse_correlation_buckets(raw: str) -> dict[str, str]:
    """Parse ``SYMBOL:bucket,SYMBOL:bucket`` into a dict.

    Unknown or malformed entries are silently skipped. Symbols without an
    explicit bucket default to their own name downstream.
    """

    result: dict[str, str] = {}
    if not raw.strip():
        return result
    for entry in raw.split(","):
        if ":" not in entry:
            continue
        sym, bucket = entry.split(":", 1)
        sym = sym.strip().upper()
        bucket = bucket.strip().lower()
        if sym and bucket:
            result[sym] = bucket
    return result


def resolve_repo_path(value: str) -> str:
    raw_path = Path(value)
    if raw_path.is_absolute():
        return str(raw_path)
    return str((REPO_ROOT / raw_path).resolve())


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
    symbols: tuple[str, ...]
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
    redis_url: str
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
    max_concurrent_positions: int = 1
    max_total_margin_usdt: float = 0.0
    correlation_buckets: dict[str, str] = dataclasses.field(default_factory=dict)
    max_per_bucket: int = 1
    session_hours_utc: str = ""
    funding_rate_abs_max: float = 0.0
    open_type: int = 1
    position_mode: int = 2
    # P1 (third assessment) §5 #3 — separate Redis key for a long-window
    # backtest "seed" calibration, consulted as a fallback when the live
    # calibration (``calibration_redis_key``) fails the freshness or
    # sample-size gate. Empty string disables the fallback.
    calibration_seed_redis_key: str = ""
    # P1 §6 #5 — cross-bot synergy: Redis key the futures bot publishes
    # funding-rate observations under, consumed by the spot bot's funding
    # carry sleeve (mexc-bot-v2/mexcbot/funding_carry.py).
    funding_observations_redis_key: str = "mexc_funding_observations"
    crypto_event_overlay_enabled: bool = True
    crypto_event_redis_key: str = "mexc:crypto_event_intelligence"
    crypto_event_refresh_seconds: int = 300
    crypto_event_stale_seconds: int = 1800
    crypto_event_min_abs_bias: float = 0.35
    crypto_event_threshold_relief: float = 4.0
    crypto_event_score_boost: float = 5.0
    crypto_event_adverse_score_penalty: float = 4.0

    @classmethod
    def from_env(cls) -> "FuturesConfig":
        hourly_check_seconds = env_int("FUTURES_HOURLY_CHECK_SECONDS", 300)
        primary_symbol = env_str("FUTURES_SYMBOL", "BTC_USDT").upper()
        symbols = parse_symbol_list(env_str("FUTURES_SYMBOLS", ""), primary_symbol)
        # P0 fix (assessment §3.2): fail fast on misnamed per-symbol env vars
        # (e.g. FUTURES_PEPE_USDT_LEVERAGE_MAX is silently ignored because
        # _symbol_env_prefix strips underscores -> FUTURES_PEPEUSDT). This
        # prevents silent leverage-cap / funding-gate bypasses in production.
        _enforce_symbol_env_key_hygiene(symbols)
        instance = cls(
            api_key=env_str("MEXC_API_KEY", ""),
            api_secret=env_str("MEXC_API_SECRET", ""),
            telegram_token=env_str("FUTURES_TELEGRAM_TOKEN", env_str("TELEGRAM_TOKEN", "")),
            telegram_chat_id=env_str("FUTURES_TELEGRAM_CHAT_ID", env_str("TELEGRAM_CHAT_ID", "")),
            paper_trade=env_bool("FUTURES_PAPER_TRADE", True),
            symbol=symbols[0],
            symbols=symbols,
            futures_base_url=env_str("MEXC_FUTURES_BASE_URL", "https://contract.mexc.com"),
            margin_budget_usdt=env_float("FUTURES_MARGIN_BUDGET_USDT", 75.0),
            max_margin_fraction=env_float("FUTURES_MAX_MARGIN_FRACTION", 0.85),
            min_confidence_score=env_float("FUTURES_SCORE_THRESHOLD", 56.0),
            hourly_check_seconds=hourly_check_seconds,
            heartbeat_seconds=env_int("FUTURES_HEARTBEAT_SECONDS", env_int("HEARTBEAT_SECONDS", 3600)),
            calibration_file=resolve_repo_path(env_str("FUTURES_CALIBRATION_FILE", "backtest_output/calibration.json")),
            calibration_redis_key=env_str("FUTURES_CALIBRATION_REDIS_KEY", "mexc_futures_calibration"),
            calibration_refresh_seconds=env_int("FUTURES_CALIBRATION_REFRESH_SECONDS", 900),
            calibration_max_age_hours=env_float("FUTURES_CALIBRATION_MAX_AGE_HOURS", 72.0),
            calibration_min_total_trades=env_int("FUTURES_CALIBRATION_MIN_TOTAL_TRADES", 15),
            calibration_seed_redis_key=env_str(
                "FUTURES_CALIBRATION_SEED_REDIS_KEY", "mexc_futures_calibration_seed"
            ),
            review_file=resolve_repo_path(env_str("FUTURES_DAILY_REVIEW_FILE", "backtest_output/daily_review.json")),
            review_redis_key=env_str("FUTURES_DAILY_REVIEW_REDIS_KEY", "mexc_futures_daily_review"),
            funding_observations_redis_key=env_str(
                "FUTURES_FUNDING_OBSERVATIONS_REDIS_KEY", "mexc_funding_observations"
            ),
            crypto_event_overlay_enabled=env_bool("FUTURES_CRYPTO_EVENT_OVERLAY_ENABLED", True),
            crypto_event_redis_key=env_str("FUTURES_CRYPTO_EVENT_REDIS_KEY", "mexc:crypto_event_intelligence"),
            crypto_event_refresh_seconds=env_int("FUTURES_CRYPTO_EVENT_REFRESH_SECONDS", 300),
            crypto_event_stale_seconds=env_int("FUTURES_CRYPTO_EVENT_STALE_SECONDS", 1800),
            crypto_event_min_abs_bias=env_float("FUTURES_CRYPTO_EVENT_MIN_ABS_BIAS", 0.35),
            crypto_event_threshold_relief=env_float("FUTURES_CRYPTO_EVENT_THRESHOLD_RELIEF", 4.0),
            crypto_event_score_boost=env_float("FUTURES_CRYPTO_EVENT_SCORE_BOOST", 5.0),
            crypto_event_adverse_score_penalty=env_float("FUTURES_CRYPTO_EVENT_ADVERSE_SCORE_PENALTY", 4.0),
            redis_url=env_str("REDIS_URL", ""),
            anthropic_api_key=env_str("ANTHROPIC_API_KEY", ""),
            runtime_state_file=resolve_repo_path(env_str("FUTURES_RUNTIME_STATE_FILE", "futures_runtime_state.json")),
            status_file=resolve_repo_path(env_str("FUTURES_STATUS_FILE", "futures_runtime_status.json")),
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
            max_concurrent_positions=max(1, env_int("FUTURES_MAX_CONCURRENT_POSITIONS", 1)),
            max_total_margin_usdt=env_float("FUTURES_MAX_TOTAL_MARGIN_USDT", 0.0),
            correlation_buckets=parse_correlation_buckets(env_str("FUTURES_CORRELATION_BUCKETS", "")),
            max_per_bucket=max(1, env_int("FUTURES_MAX_PER_BUCKET", 1)),
            session_hours_utc=env_str("FUTURES_SESSION_HOURS_UTC", ""),
            # P1 fix (assessment §4.2 / §6 #6): tighten the global funding-rate cap
            # default. The previous 0.0008/8h (≈87%/yr APR) is permissive to the
            # point of being decorative on a momentum strategy whose holds straddle
            # one funding interval. 0.0002/8h (≈22%/yr) keeps the gate meaningful.
            # Per-symbol overrides remain authoritative (e.g. PEPE memecoin can be
            # widened back to 0.00025 via FUTURES_PEPEUSDT_FUNDING_RATE_ABS_MAX).
            funding_rate_abs_max=env_float("FUTURES_FUNDING_RATE_ABS_MAX", 0.0002),
            open_type=env_int("FUTURES_OPEN_TYPE", 1),
            position_mode=env_int("FUTURES_POSITION_MODE", 2),
        )
        # Sprint 1 — §2.4 strict recv_window. Opt-in via USE_STRICT_RECV_WINDOW=1
        # clamps recv_window_seconds to the institutional-standard 5s.
        if env_bool("USE_STRICT_RECV_WINDOW", False):
            strict_cap = env_int("STRICT_RECV_WINDOW_SECONDS", 5)
            if strict_cap > 0:
                instance = dataclasses.replace(
                    instance, recv_window_seconds=min(instance.recv_window_seconds, strict_cap)
                )
        # Sprint 1 — §2.6 tight hard-loss cap. Opt-in via USE_HARD_LOSS_CAP_TIGHT=1
        # clamps hard_loss_cap_pct to HARD_LOSS_CAP_TIGHT_PCT (default 0.40) so a
        # single trade cannot lose more than that fraction of posted margin.
        if env_bool("USE_HARD_LOSS_CAP_TIGHT", False):
            tight_cap = env_float("HARD_LOSS_CAP_TIGHT_PCT", 0.40)
            if tight_cap > 0:
                instance = dataclasses.replace(
                    instance, hard_loss_cap_pct=min(instance.hard_loss_cap_pct, tight_cap)
                )
        return instance

    def for_symbol(self, symbol: str) -> "FuturesConfig":
        """Return a copy of this config scoped to ``symbol`` with per-symbol env overrides.

        Looks up ``FUTURES_<SYMBOL>_<PARAM>`` env vars (symbol with non-alphanumerics
        stripped) for each tunable strategy / risk parameter. Missing overrides fall
        back to the default per-symbol profile first, then the base config.
        """

        sym = symbol.upper()
        profile = _default_symbol_profile(sym)
        if sym == self.symbol and not _has_symbol_overrides(sym) and not profile:
            return self

        def prof_float(field_name: str, fallback: float) -> float:
            raw = profile.get(field_name)
            try:
                return float(raw) if raw is not None else fallback
            except (TypeError, ValueError):
                return fallback

        def prof_int(field_name: str, fallback: int) -> int:
            raw = profile.get(field_name)
            try:
                return int(raw) if raw is not None else fallback
            except (TypeError, ValueError):
                return fallback

        leverage_max = env_int_for_symbol(sym, "LEVERAGE_MAX", prof_int("leverage_max", self.leverage_max))
        leverage_min = min(env_int_for_symbol(sym, "LEVERAGE_MIN", prof_int("leverage_min", self.leverage_min)), leverage_max)
        return dataclasses.replace(
            self,
            symbol=sym,
            leverage_min=leverage_min,
            leverage_max=leverage_max,
            min_confidence_score=env_float_for_symbol(sym, "SCORE_THRESHOLD", prof_float("min_confidence_score", self.min_confidence_score)),
            hard_loss_cap_pct=env_float_for_symbol(sym, "HARD_LOSS_CAP_PCT", prof_float("hard_loss_cap_pct", self.hard_loss_cap_pct)),
            adx_floor=env_float_for_symbol(sym, "ADX_FLOOR", prof_float("adx_floor", self.adx_floor)),
            trend_24h_floor=env_float_for_symbol(sym, "TREND_24H_FLOOR", prof_float("trend_24h_floor", self.trend_24h_floor)),
            trend_6h_floor=env_float_for_symbol(sym, "TREND_6H_FLOOR", prof_float("trend_6h_floor", self.trend_6h_floor)),
            breakout_buffer_atr=env_float_for_symbol(sym, "BREAKOUT_BUFFER_ATR", prof_float("breakout_buffer_atr", self.breakout_buffer_atr)),
            consolidation_window_bars=env_int_for_symbol(sym, "CONSOLIDATION_WINDOW_BARS", prof_int("consolidation_window_bars", self.consolidation_window_bars)),
            consolidation_max_range_pct=env_float_for_symbol(sym, "CONSOLIDATION_MAX_RANGE_PCT", prof_float("consolidation_max_range_pct", self.consolidation_max_range_pct)),
            consolidation_atr_mult=env_float_for_symbol(sym, "CONSOLIDATION_ATR_MULT", prof_float("consolidation_atr_mult", self.consolidation_atr_mult)),
            volume_ratio_floor=env_float_for_symbol(sym, "VOLUME_RATIO_FLOOR", prof_float("volume_ratio_floor", self.volume_ratio_floor)),
            tp_atr_mult=env_float_for_symbol(sym, "TP_ATR_MULT", prof_float("tp_atr_mult", self.tp_atr_mult)),
            tp_range_mult=env_float_for_symbol(sym, "TP_RANGE_MULT", prof_float("tp_range_mult", self.tp_range_mult)),
            tp_floor_pct=env_float_for_symbol(sym, "TP_FLOOR_PCT", prof_float("tp_floor_pct", self.tp_floor_pct)),
            sl_buffer_atr_mult=env_float_for_symbol(sym, "SL_BUFFER_ATR_MULT", prof_float("sl_buffer_atr_mult", self.sl_buffer_atr_mult)),
            sl_trend_atr_mult=env_float_for_symbol(sym, "SL_TREND_ATR_MULT", prof_float("sl_trend_atr_mult", self.sl_trend_atr_mult)),
            min_reward_risk=env_float_for_symbol(sym, "MIN_REWARD_RISK", prof_float("min_reward_risk", self.min_reward_risk)),
            early_exit_tp_progress=env_float_for_symbol(sym, "EARLY_EXIT_TP_PROGRESS", prof_float("early_exit_tp_progress", self.early_exit_tp_progress)),
            early_exit_min_profit_pct=env_float_for_symbol(sym, "EARLY_EXIT_MIN_PROFIT_PCT", prof_float("early_exit_min_profit_pct", self.early_exit_min_profit_pct)),
            early_exit_buffer_pct=env_float_for_symbol(sym, "EARLY_EXIT_BUFFER_PCT", prof_float("early_exit_buffer_pct", self.early_exit_buffer_pct)),
            session_hours_utc=env_str_for_symbol(sym, "SESSION_HOURS_UTC", self.session_hours_utc),
            funding_rate_abs_max=env_float_for_symbol(sym, "FUNDING_RATE_ABS_MAX", prof_float("funding_rate_abs_max", self.funding_rate_abs_max)),
        )


_SYMBOL_OVERRIDE_SUFFIXES: tuple[str, ...] = (
    "LEVERAGE_MIN",
    "LEVERAGE_MAX",
    "SCORE_THRESHOLD",
    "HARD_LOSS_CAP_PCT",
    "ADX_FLOOR",
    "TREND_24H_FLOOR",
    "TREND_6H_FLOOR",
    "BREAKOUT_BUFFER_ATR",
    "CONSOLIDATION_WINDOW_BARS",
    "CONSOLIDATION_MAX_RANGE_PCT",
    "CONSOLIDATION_ATR_MULT",
    "VOLUME_RATIO_FLOOR",
    "TP_ATR_MULT",
    "TP_RANGE_MULT",
    "TP_FLOOR_PCT",
    "SL_BUFFER_ATR_MULT",
    "SL_TREND_ATR_MULT",
    "MIN_REWARD_RISK",
    "EARLY_EXIT_TP_PROGRESS",
    "EARLY_EXIT_MIN_PROFIT_PCT",
    "EARLY_EXIT_BUFFER_PCT",
    "SESSION_HOURS_UTC",
    "FUNDING_RATE_ABS_MAX",
)


def _has_symbol_overrides(symbol: str) -> bool:
    prefix = _symbol_env_prefix(symbol)
    for suffix in _SYMBOL_OVERRIDE_SUFFIXES:
        if os.getenv(f"{prefix}_{suffix}"):
            return True
    return False


def _default_symbol_profile(symbol: str) -> dict[str, float | int]:
    if not env_bool("FUTURES_SYMBOL_PROFILES_ENABLED", True):
        return {}
    return dict(DEFAULT_SYMBOL_PARAMETER_PROFILES.get(symbol.upper(), {}))


def detect_misnamed_symbol_env_keys(symbols: tuple[str, ...]) -> list[tuple[str, str]]:
    """P0 fix (assessment §3.2): detect per-symbol env vars that use the natural
    underscore form (e.g. ``FUTURES_PEPE_USDT_LEVERAGE_MAX``) instead of the
    canonical alphanumeric form expected by ``_symbol_env_prefix``
    (``FUTURES_PEPEUSDT_LEVERAGE_MAX``). Such vars are silently ignored at
    runtime, which can nullify a per-symbol leverage cap or funding gate
    without warning.

    Returns a list of ``(misnamed_key, suggested_canonical_key)`` pairs.
    Empty list means clean.
    """

    findings: list[tuple[str, str]] = []
    for symbol in symbols:
        sym_upper = symbol.upper()
        canonical_prefix = _symbol_env_prefix(sym_upper)  # e.g. FUTURES_PEPEUSDT
        natural_prefix = f"FUTURES_{sym_upper}"           # e.g. FUTURES_PEPE_USDT
        if natural_prefix == canonical_prefix:
            continue  # symbol has no underscores (e.g. BTCUSDT) -> no ambiguity
        for suffix in _SYMBOL_OVERRIDE_SUFFIXES:
            misnamed = f"{natural_prefix}_{suffix}"
            canonical = f"{canonical_prefix}_{suffix}"
            if os.getenv(misnamed) is not None and os.getenv(canonical) is None:
                findings.append((misnamed, canonical))
    return findings


class MisnamedSymbolEnvKeyError(ValueError):
    """Raised at boot when per-symbol env vars use a non-canonical form."""


def _enforce_symbol_env_key_hygiene(symbols: tuple[str, ...]) -> None:
    if env_bool("FUTURES_DISABLE_ENV_KEY_VALIDATION", False):
        return
    findings = detect_misnamed_symbol_env_keys(symbols)
    if not findings:
        return
    lines = [f"  - {bad}  ->  {good}" for bad, good in findings]
    raise MisnamedSymbolEnvKeyError(
        "Per-symbol env vars use non-canonical form (underscores in symbol are "
        "stripped by _symbol_env_prefix, so the keys below are silently ignored). "
        "Rename them to the canonical form, or set "
        "FUTURES_DISABLE_ENV_KEY_VALIDATION=1 to bypass (not recommended):\n"
        + "\n".join(lines)
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
            output_dir=resolve_repo_path(env_str("FUTURES_BACKTEST_OUTPUT_DIR", "backtest_output")),
            cache_dir=resolve_repo_path(env_str("FUTURES_BACKTEST_CACHE_DIR", "backtest_cache")),
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