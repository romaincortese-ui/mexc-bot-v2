from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CryptoEventOverlayDecision:
    allowed: bool
    reason: str
    fresh: bool = False
    bias_score: float = 0.0
    threshold_relief: float = 0.0
    score_offset: float = 0.0
    event_count: int = 0
    max_severity: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def is_crypto_event_state_fresh(
    state: Mapping[str, Any] | None,
    now: datetime,
    *,
    max_age_seconds: int,
) -> bool:
    if not state:
        return False
    generated_at = parse_event_timestamp(
        state.get("generated_at") or state.get("as_of") or state.get("updated_at")
    )
    if generated_at is None:
        return False
    age_seconds = (now.astimezone(timezone.utc) - generated_at).total_seconds()
    return 0 <= age_seconds <= max_age_seconds


def evaluate_crypto_event_overlay(
    state: Mapping[str, Any] | None,
    *,
    symbol: str,
    now: datetime,
    side: str | None = None,
    stale_seconds: int = 1800,
    min_abs_bias: float = 0.35,
    threshold_relief_points: float = 4.0,
    score_boost_points: float = 5.0,
    adverse_score_penalty_points: float = 4.0,
) -> CryptoEventOverlayDecision:
    if not is_crypto_event_state_fresh(state, now, max_age_seconds=stale_seconds):
        return CryptoEventOverlayDecision(allowed=True, reason="no_fresh_crypto_event_state")

    bias_score, event_count, max_severity, event_titles = _crypto_bias_for_symbol(state or {}, symbol)
    metadata = {
        "crypto_event_bias": round(bias_score, 4),
        "crypto_event_count": event_count,
        "crypto_event_max_severity": round(max_severity, 3),
        "crypto_event_titles": event_titles[:3],
    }
    if abs(bias_score) < min_abs_bias:
        return CryptoEventOverlayDecision(
            allowed=True,
            reason="crypto_event_bias_neutral",
            fresh=True,
            bias_score=bias_score,
            event_count=event_count,
            max_severity=max_severity,
            metadata=metadata,
        )

    if side is None:
        relief = threshold_relief_points * min(1.0, abs(bias_score))
        metadata["crypto_event_threshold_relief"] = round(relief, 3)
        return CryptoEventOverlayDecision(
            allowed=True,
            reason="crypto_event_threshold_relief",
            fresh=True,
            bias_score=bias_score,
            threshold_relief=relief,
            event_count=event_count,
            max_severity=max_severity,
            metadata=metadata,
        )

    side_sign = 1.0 if side.upper() == "LONG" else -1.0 if side.upper() == "SHORT" else 0.0
    alignment = side_sign * bias_score
    metadata["crypto_event_alignment"] = round(alignment, 4)
    if max_severity >= 1.0 and alignment <= -0.85:
        return CryptoEventOverlayDecision(
            allowed=False,
            reason="extreme_crypto_event_adverse",
            fresh=True,
            bias_score=bias_score,
            event_count=event_count,
            max_severity=max_severity,
            metadata=metadata,
        )
    if alignment >= min_abs_bias:
        offset = score_boost_points * min(1.0, alignment)
        metadata["crypto_event_score_offset"] = round(offset, 3)
        return CryptoEventOverlayDecision(
            allowed=True,
            reason="crypto_event_favourable_boost",
            fresh=True,
            bias_score=bias_score,
            score_offset=offset,
            event_count=event_count,
            max_severity=max_severity,
            metadata=metadata,
        )
    if alignment <= -min_abs_bias:
        offset = -adverse_score_penalty_points * min(1.0, abs(alignment))
        metadata["crypto_event_score_offset"] = round(offset, 3)
        return CryptoEventOverlayDecision(
            allowed=True,
            reason="crypto_event_adverse_reduce",
            fresh=True,
            bias_score=bias_score,
            score_offset=offset,
            event_count=event_count,
            max_severity=max_severity,
            metadata=metadata,
        )
    return CryptoEventOverlayDecision(
        allowed=True,
        reason="crypto_event_direction_neutral",
        fresh=True,
        bias_score=bias_score,
        event_count=event_count,
        max_severity=max_severity,
        metadata=metadata,
    )


def parse_event_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _crypto_bias_for_symbol(state: Mapping[str, Any], symbol: str) -> tuple[float, int, float, list[str]]:
    components: list[tuple[float, float]] = []
    max_severity = 0.0
    titles: list[str] = []
    symbol_norm = _normalize_symbol(symbol)
    raw_events = state.get("events") or []
    if isinstance(raw_events, list):
        for event in raw_events:
            if not isinstance(event, Mapping):
                continue
            direction = _direction_score(str(event.get("direction") or event.get("bias") or ""))
            if abs(direction) < 1e-9:
                continue
            relevance = _event_relevance(event, symbol_norm)
            if relevance <= 0:
                continue
            severity = _severity_weight(event.get("severity") or event.get("impact"))
            max_severity = max(max_severity, severity)
            components.append((direction * relevance * severity, relevance))
            title = str(event.get("title") or event.get("reason") or "")
            if title:
                titles.append(title)

    market_risk_score = _optional_float(state.get("market_risk_score"))
    if market_risk_score is not None and market_risk_score > 0:
        risk_component = -min(1.0, market_risk_score)
        components.append((risk_component, 0.6))
        max_severity = max(max_severity, min(1.0, market_risk_score))

    total_weight = sum(weight for _score, weight in components if weight > 0)
    if total_weight <= 0:
        return 0.0, 0, max_severity, titles
    score = sum(value * weight for value, weight in components) / total_weight
    return _clamp(score, -1.0, 1.0), len(components), max_severity, titles


def _event_relevance(event: Mapping[str, Any], symbol_norm: str) -> float:
    raw_symbols = event.get("symbols") or []
    if isinstance(raw_symbols, str):
        raw_symbols = [raw_symbols]
    symbols = {_normalize_symbol(str(item)) for item in raw_symbols if str(item).strip()}
    if symbols:
        return 1.2 if symbol_norm in symbols else 0.0
    scope = str(event.get("scope") or "").lower()
    if not scope or scope in {"market", "global", "crypto", "all", "sector"}:
        return 1.0
    return 0.0


def _direction_score(value: str) -> float:
    lowered = value.lower()
    if lowered in {"risk_on", "bullish", "positive", "long"}:
        return 1.0
    if lowered in {"risk_off", "bearish", "negative", "short"}:
        return -1.0
    return 0.0


def _severity_weight(value: Any) -> float:
    if isinstance(value, (int, float)):
        return _clamp(float(value), 0.2, 1.2)
    lowered = str(value or "").lower()
    if lowered in {"critical", "extreme", "severe"}:
        return 1.2
    if lowered == "high":
        return 1.0
    if lowered == "medium":
        return 0.7
    if lowered == "low":
        return 0.4
    return 0.7


def _normalize_symbol(symbol: str) -> str:
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
