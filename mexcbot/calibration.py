from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

try:
    import redis
except ImportError:
    redis = None  # type: ignore

from mexcbot.exits import get_exit_profile
from mexcbot.models import Opportunity


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _profit_factor(pnl: pd.Series) -> float:
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return float(wins.sum() / abs(losses.sum())) if not losses.empty else 999.0


def _summarize_trade_group(group: pd.DataFrame) -> dict[str, Any]:
    pnl = group["pnl_usdt"].astype(float)
    return {
        "trades": int(len(group)),
        "win_rate": float((pnl > 0).mean()),
        "total_pnl": float(pnl.sum()),
        "profit_factor": _profit_factor(pnl),
        "expectancy": float(pnl.mean()),
    }


def _group_trade_metrics(trades_df: pd.DataFrame, keys: list[str]) -> dict[str, Any]:
    grouped: dict[str, Any] = {}
    if trades_df.empty:
        return grouped
    normalized = trades_df.copy()
    for key in keys:
        normalized[key] = normalized.get(key, "UNKNOWN")
        normalized[key] = normalized[key].fillna("UNKNOWN").astype(str)
    for raw_keys, group in normalized.groupby(keys):
        if not isinstance(raw_keys, tuple):
            raw_keys = (raw_keys,)
        node = grouped
        for key in raw_keys[:-1]:
            node = node.setdefault(str(key), {})
        node[str(raw_keys[-1])] = _summarize_trade_group(group)
    return grouped


def _derive_entry_adjustment(metrics: Mapping[str, Any], *, min_trades: int) -> dict[str, Any]:
    trades = int(metrics.get("trades", 0) or 0)
    if trades < min_trades:
        return {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None}

    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    expectancy = float(metrics.get("expectancy", 0.0) or 0.0)
    win_rate = float(metrics.get("win_rate", 0.0) or 0.0)

    if trades >= max(20, min_trades * 2) and profit_factor < 0.7 and expectancy < -0.03 and win_rate < 0.4:
        return {
            "threshold_offset": 6.0,
            "risk_mult": 0.5,
            "block_reason": "calibration block: persistent underperformance",
        }

    if profit_factor < 0.95 or expectancy < 0:
        tighten = min(6.0, round(max(0.0, (1.0 - profit_factor) * 10.0) + max(0.0, -expectancy) * 20.0, 2))
        risk_mult = max(0.5, round(1.0 - min(0.45, tighten / 12.0), 2))
        return {"threshold_offset": tighten, "risk_mult": risk_mult, "block_reason": None}

    if profit_factor > 1.15 and expectancy > 0.02 and win_rate > 0.5:
        relax = min(3.0, round((profit_factor - 1.0) * 5.0 + min(1.0, expectancy * 10.0), 2))
        risk_mult = min(1.25, round(1.0 + min(0.25, relax / 10.0), 2))
        return {"threshold_offset": -relax, "risk_mult": risk_mult, "block_reason": None}

    return {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None}


def _derive_exit_adjustment(metrics: Mapping[str, Any], *, min_trades: int) -> dict[str, Any]:
    trades = int(metrics.get("trades", 0) or 0)
    if trades < min_trades:
        return {}

    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    expectancy = float(metrics.get("expectancy", 0.0) or 0.0)

    if profit_factor > 1.15 and expectancy > 0.02:
        return {
            "breakeven_activation_mult": 1.05,
            "trail_activation_mult": 1.08,
            "trail_pct_mult": 1.12,
            "partial_tp_ratio_offset": -0.10,
            "flat_max_minutes_mult": 1.15,
        }

    if profit_factor < 0.95 or expectancy < 0:
        return {
            "breakeven_activation_mult": 0.90,
            "trail_activation_mult": 0.92,
            "trail_pct_mult": 0.88,
            "partial_tp_ratio_offset": 0.10,
            "flat_max_minutes_mult": 0.85,
        }

    return {}


def build_trade_calibration(
    trades: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    min_strategy_trades: int = 12,
    min_symbol_trades: int = 8,
) -> dict[str, Any]:
    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return {
            "generated_at": _utc_now().isoformat(),
            "window_start": window_start.astimezone(timezone.utc).isoformat(),
            "window_end": window_end.astimezone(timezone.utc).isoformat(),
            "total_trades": 0,
            "by_strategy": {},
            "by_strategy_signal": {},
            "by_strategy_symbol": {},
            "by_strategy_symbol_signal": {},
            "entry_adjustments": {
                "by_strategy": {},
                "by_strategy_signal": {},
                "by_strategy_symbol": {},
                "by_strategy_symbol_signal": {},
            },
            "exit_adjustments": {
                "by_strategy": {},
                "by_strategy_signal": {},
                "by_strategy_symbol": {},
                "by_strategy_symbol_signal": {},
            },
        }

    normalized = trades_df.copy()
    for column in ("strategy", "symbol", "entry_signal"):
        normalized[column] = normalized.get(column, "UNKNOWN")
        normalized[column] = normalized[column].fillna("UNKNOWN").astype(str)

    by_strategy = _group_trade_metrics(normalized, ["strategy"])
    by_strategy_signal = _group_trade_metrics(normalized, ["strategy", "entry_signal"])
    by_strategy_symbol = _group_trade_metrics(normalized, ["strategy", "symbol"])
    by_strategy_symbol_signal = _group_trade_metrics(normalized, ["strategy", "symbol", "entry_signal"])

    entry_by_strategy = {
        strategy: _derive_entry_adjustment(metrics, min_trades=min_strategy_trades)
        for strategy, metrics in by_strategy.items()
    }
    exit_by_strategy = {
        strategy: _derive_exit_adjustment(metrics, min_trades=min_strategy_trades)
        for strategy, metrics in by_strategy.items()
        if _derive_exit_adjustment(metrics, min_trades=min_strategy_trades)
    }

    entry_by_strategy_signal: dict[str, dict[str, Any]] = {}
    exit_by_strategy_signal: dict[str, dict[str, Any]] = {}
    for strategy, signals in by_strategy_signal.items():
        for signal, metrics in signals.items():
            entry_adjustment = _derive_entry_adjustment(metrics, min_trades=min_strategy_trades)
            exit_adjustment = _derive_exit_adjustment(metrics, min_trades=min_strategy_trades)
            if entry_adjustment != {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None}:
                entry_by_strategy_signal.setdefault(strategy, {})[signal] = entry_adjustment
            if exit_adjustment:
                exit_by_strategy_signal.setdefault(strategy, {})[signal] = exit_adjustment

    entry_by_strategy_symbol: dict[str, dict[str, Any]] = {}
    exit_by_strategy_symbol: dict[str, dict[str, Any]] = {}
    for strategy, symbols in by_strategy_symbol.items():
        for symbol, metrics in symbols.items():
            entry_adjustment = _derive_entry_adjustment(metrics, min_trades=min_symbol_trades)
            exit_adjustment = _derive_exit_adjustment(metrics, min_trades=min_symbol_trades)
            if entry_adjustment != {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None}:
                entry_by_strategy_symbol.setdefault(strategy, {})[symbol] = entry_adjustment
            if exit_adjustment:
                exit_by_strategy_symbol.setdefault(strategy, {})[symbol] = exit_adjustment

    entry_by_strategy_symbol_signal: dict[str, dict[str, dict[str, Any]]] = {}
    exit_by_strategy_symbol_signal: dict[str, dict[str, dict[str, Any]]] = {}
    for strategy, symbols in by_strategy_symbol_signal.items():
        for symbol, signals in symbols.items():
            for signal, metrics in signals.items():
                entry_adjustment = _derive_entry_adjustment(metrics, min_trades=min_symbol_trades)
                exit_adjustment = _derive_exit_adjustment(metrics, min_trades=min_symbol_trades)
                if entry_adjustment != {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None}:
                    entry_by_strategy_symbol_signal.setdefault(strategy, {}).setdefault(symbol, {})[signal] = entry_adjustment
                if exit_adjustment:
                    exit_by_strategy_symbol_signal.setdefault(strategy, {}).setdefault(symbol, {})[signal] = exit_adjustment

    return {
        "generated_at": _utc_now().isoformat(),
        "window_start": window_start.astimezone(timezone.utc).isoformat(),
        "window_end": window_end.astimezone(timezone.utc).isoformat(),
        "total_trades": int(len(normalized)),
        "by_strategy": by_strategy,
        "by_strategy_signal": by_strategy_signal,
        "by_strategy_symbol": by_strategy_symbol,
        "by_strategy_symbol_signal": by_strategy_symbol_signal,
        "entry_adjustments": {
            "by_strategy": entry_by_strategy,
            "by_strategy_signal": entry_by_strategy_signal,
            "by_strategy_symbol": entry_by_strategy_symbol,
            "by_strategy_symbol_signal": entry_by_strategy_symbol_signal,
        },
        "exit_adjustments": {
            "by_strategy": exit_by_strategy,
            "by_strategy_signal": exit_by_strategy_signal,
            "by_strategy_symbol": exit_by_strategy_symbol,
            "by_strategy_symbol_signal": exit_by_strategy_symbol_signal,
        },
    }


def write_trade_calibration(file_path: str, calibration: Mapping[str, Any]) -> None:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calibration, indent=2), encoding="utf-8")


def publish_trade_calibration(redis_url: str, redis_key: str, calibration: Mapping[str, Any]) -> bool:
    if not redis_url or not redis_key or redis is None:
        return False
    client = redis.from_url(redis_url)
    client.set(redis_key, json.dumps(calibration))
    return True


def trade_calibration_hash(calibration: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in dict(calibration).items() if key != "calibration_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summarize_trade_calibration(calibration: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, float | int]] = {}
    for strategy, metrics in dict(calibration.get("by_strategy", {})).items():
        metric_map = dict(metrics or {})
        by_strategy[str(strategy).upper()] = {
            "trades": int(metric_map.get("trades", 0) or 0),
            "profit_factor": round(float(metric_map.get("profit_factor", 0.0) or 0.0), 4),
            "total_pnl": round(float(metric_map.get("total_pnl", 0.0) or 0.0), 4),
            "expectancy": round(float(metric_map.get("expectancy", 0.0) or 0.0), 6),
        }
    return {
        "source": source or "",
        "calibration_hash": trade_calibration_hash(calibration),
        "generated_at": str(calibration.get("generated_at") or ""),
        "window_start": str(calibration.get("window_start") or ""),
        "window_end": str(calibration.get("window_end") or ""),
        "total_trades": int(calibration.get("total_trades", 0) or 0),
        "by_strategy": by_strategy,
    }


def format_trade_calibration_manifest(manifest: Mapping[str, Any]) -> str:
    if not manifest:
        return "none loaded"
    by_strategy = dict(manifest.get("by_strategy", {}) or {})
    strategy_bits = []
    for strategy, metrics in sorted(by_strategy.items()):
        metric_map = dict(metrics or {})
        strategy_bits.append(
            f"{strategy}:n={int(metric_map.get('trades', 0) or 0)}/PF={float(metric_map.get('profit_factor', 0.0) or 0.0):.2f}"
        )
    strategies = ", ".join(strategy_bits) if strategy_bits else "no strategy metrics"
    calibration_hash = str(manifest.get("calibration_hash") or "")[:12] or "n/a"
    window_start = str(manifest.get("window_start") or "?")[:10]
    window_end = str(manifest.get("window_end") or "?")[:10]
    source = str(manifest.get("source") or "unknown source")
    return (
        f"hash={calibration_hash} source={source} window={window_start}..{window_end} "
        f"trades={int(manifest.get('total_trades', 0) or 0)} strategies={strategies}"
    )


def validate_trade_calibration_payload(data: Mapping[str, Any], *, max_age_hours: float, min_total_trades: int) -> tuple[bool, str | None]:
    total_trades = int(data.get("total_trades", 0) or 0)
    if total_trades < min_total_trades:
        return False, f"insufficient sample ({total_trades} trades < {min_total_trades})"

    generated_at = data.get("generated_at")
    if not generated_at:
        return False, "missing generated_at"
    try:
        created = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return False, "invalid generated_at"

    age_hours = (_utc_now() - created).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return False, f"stale calibration ({age_hours:.1f}h > {max_age_hours:.1f}h)"
    return True, None


def load_trade_calibration(*, redis_url: str, redis_key: str, file_path: str) -> tuple[dict[str, Any] | None, str | None]:
    if redis_url and redis_key and redis is not None:
        try:
            client = redis.from_url(redis_url)
            raw = client.get(redis_key)
            if raw:
                return json.loads(raw), f"Redis key {redis_key}"
        except Exception:
            pass

    path = Path(file_path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), str(path)
        except Exception:
            return None, None
    return None, None


def _lookup_adjustment(section: Mapping[str, Any], strategy: str, symbol: str, entry_signal: str | None = None) -> dict[str, Any]:
    strategy_name = strategy.upper()
    symbol_name = symbol.upper()
    signal_name = (entry_signal or "").upper()

    strategy_adjustment = dict(section.get("by_strategy", {}).get(strategy_name, {}))
    strategy_signal_adjustment = dict(section.get("by_strategy_signal", {}).get(strategy_name, {}).get(signal_name, {})) if signal_name else {}
    symbol_adjustment = dict(section.get("by_strategy_symbol", {}).get(strategy_name, {}).get(symbol_name, {}))
    symbol_signal_adjustment = (
        dict(section.get("by_strategy_symbol_signal", {}).get(strategy_name, {}).get(symbol_name, {}).get(signal_name, {}))
        if signal_name
        else {}
    )
    merged = {**strategy_adjustment, **strategy_signal_adjustment, **symbol_adjustment, **symbol_signal_adjustment}
    if merged:
        if symbol_signal_adjustment:
            merged["source"] = "pair_signal"
        elif symbol_adjustment:
            merged["source"] = "pair"
        elif strategy_signal_adjustment:
            merged["source"] = "strategy_signal"
        else:
            merged["source"] = "strategy"
    return merged


def get_entry_adjustment(
    calibration: Mapping[str, Any] | None,
    strategy: str,
    symbol: str,
    entry_signal: str | None = None,
) -> dict[str, Any]:
    if not calibration:
        return {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None, "source": None}
    merged = _lookup_adjustment(calibration.get("entry_adjustments", {}), strategy, symbol, entry_signal)
    if not merged:
        return {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None, "source": None}
    merged.setdefault("threshold_offset", 0.0)
    merged.setdefault("risk_mult", 1.0)
    merged.setdefault("block_reason", None)
    return merged


def resolve_exit_profile_override(
    calibration: Mapping[str, Any] | None,
    strategy: str,
    symbol: str,
    entry_signal: str | None = None,
) -> dict[str, float | int]:
    if not calibration:
        return {}
    adjustment = _lookup_adjustment(calibration.get("exit_adjustments", {}), strategy, symbol, entry_signal)
    if not adjustment:
        return {}

    base = get_exit_profile(strategy, entry_signal=entry_signal)
    override: dict[str, float | int] = {}

    if "breakeven_activation_mult" in adjustment:
        override["breakeven_activation_pct"] = round(float(base["breakeven_activation_pct"]) * float(adjustment["breakeven_activation_mult"]), 6)
    if "trail_activation_mult" in adjustment:
        override["trail_activation_pct"] = round(float(base["trail_activation_pct"]) * float(adjustment["trail_activation_mult"]), 6)
    if "trail_pct_mult" in adjustment:
        override["trail_pct"] = round(float(base["trail_pct"]) * float(adjustment["trail_pct_mult"]), 6)
    if "partial_tp_ratio_offset" in adjustment:
        override["partial_tp_ratio"] = round(
            min(0.9, max(0.0, float(base.get("partial_tp_ratio", 0.0)) + float(adjustment["partial_tp_ratio_offset"]))),
            4,
        )
    if "flat_max_minutes_mult" in adjustment:
        override["flat_max_minutes"] = max(15, int(round(float(base["flat_max_minutes"]) * float(adjustment["flat_max_minutes_mult"]))))
    return override


def apply_opportunity_calibration(
    opportunity: Opportunity,
    calibration: Mapping[str, Any] | None,
    *,
    base_threshold: float,
) -> Opportunity | None:
    adjustment = get_entry_adjustment(calibration, opportunity.strategy, opportunity.symbol, opportunity.entry_signal)
    if adjustment.get("block_reason"):
        return None

    threshold_offset = float(adjustment.get("threshold_offset", 0.0) or 0.0)
    risk_mult = min(1.25, max(0.5, float(adjustment.get("risk_mult", 1.0) or 1.0)))
    selection_score = opportunity.score * risk_mult - threshold_offset
    effective_threshold = base_threshold + threshold_offset
    if selection_score < effective_threshold:
        return None

    metadata = dict(opportunity.metadata)
    metadata.update(
        {
            "raw_score": round(opportunity.score, 2),
            "calibration_threshold_offset": threshold_offset,
            "calibration_risk_mult": risk_mult,
            "calibration_source": adjustment.get("source"),
            "effective_threshold": round(effective_threshold, 2),
            "allocation_mult": risk_mult,
        }
    )
    exit_profile_override = resolve_exit_profile_override(
        calibration,
        opportunity.strategy,
        opportunity.symbol,
        opportunity.entry_signal,
    )
    existing_exit_profile = dict(metadata.get("exit_profile_override") or {})
    if exit_profile_override:
        existing_exit_profile.update(exit_profile_override)
    if existing_exit_profile:
        metadata["exit_profile_override"] = existing_exit_profile

    opportunity.score = round(selection_score, 2)
    opportunity.metadata = metadata
    return opportunity