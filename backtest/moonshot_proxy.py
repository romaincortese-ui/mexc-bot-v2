from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from backtest.config import BacktestConfig, interval_to_timedelta
from mexcbot.models import Opportunity
from mexcbot.strategies.moonshot import MOONSHOT_SOCIAL_BOOST_MAX, MOONSHOT_NEW_LISTING_MAX_DAYS, MOONSHOT_NEW_LISTING_MIN_DAYS, _scale_social_buzz, score_moonshot_from_frame


@dataclass(frozen=True, slots=True)
class MoonshotProxyCandidate:
    opportunity: Opportunity
    data_key: str


def _visible_days(frame: pd.DataFrame, interval: str) -> float:
    if frame is None or frame.empty:
        return 0.0
    step = interval_to_timedelta(interval)
    return (len(frame) * step.total_seconds()) / 86_400.0


def _is_recent_listing(frame: pd.DataFrame, interval: str) -> bool:
    days_visible = _visible_days(frame, interval)
    return MOONSHOT_NEW_LISTING_MIN_DAYS <= days_visible <= MOONSHOT_NEW_LISTING_MAX_DAYS


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _proxy_metrics(frame: pd.DataFrame) -> dict[str, float]:
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    current_price = float(close.iloc[-1]) if not close.empty else 0.0
    anchor_8 = float(close.iloc[-9]) if len(close) >= 9 else float(close.iloc[0])
    anchor_16 = float(close.iloc[-17]) if len(close) >= 17 else float(close.iloc[0])
    ret_8 = ((current_price / anchor_8) - 1.0) * 100.0 if anchor_8 > 0 else 0.0
    ret_16 = ((current_price / anchor_16) - 1.0) * 100.0 if anchor_16 > 0 else 0.0
    last_vol = float(volume.iloc[-1]) if not volume.empty else 0.0
    avg_8 = float(volume.tail(8).mean()) if len(volume) >= 8 else float(volume.mean() or 0.0)
    avg_20 = float(volume.tail(20).mean()) if len(volume) >= 20 else float(volume.mean() or 0.0)
    base_avg = float(volume.iloc[:-8].mean()) if len(volume) > 8 else avg_20
    burst_ratio = (avg_8 / base_avg) if base_avg > 0 else 1.0
    last_ratio = (last_vol / avg_20) if avg_20 > 0 else 1.0
    return {
        "ret_8": round(ret_8, 4),
        "ret_16": round(ret_16, 4),
        "burst_ratio": round(burst_ratio, 4),
        "last_ratio": round(last_ratio, 4),
    }


def score_backtest_moonshot_candidates(
    *,
    config: BacktestConfig,
    datasets: list[tuple[str, str, pd.DataFrame]],
    score_threshold: float,
) -> list[MoonshotProxyCandidate]:
    max_social_boost = MOONSHOT_SOCIAL_BOOST_MAX
    threshold_margin = max_social_boost
    frame_by_key = {data_key: frame for _symbol, data_key, frame in datasets}
    social_slots = 1
    scored: list[tuple[Opportunity, str, dict[str, float], bool]] = []
    for symbol, data_key, frame in datasets:
        if frame is None or len(frame) < 22:
            continue
        is_new = _is_recent_listing(frame, "5m")
        candidate = score_moonshot_from_frame(
            symbol,
            frame,
            score_threshold=score_threshold,
            is_new=is_new,
            threshold_margin=threshold_margin,
        )
        if candidate is None:
            continue
        scored.append((candidate, data_key, _proxy_metrics(frame), is_new))

    if not scored:
        return []

    re_scored = scored

    if not re_scored:
        return []

    near_threshold = [
        item
        for item in re_scored
        if item[0].score < score_threshold and item[0].score >= max(0.0, score_threshold - max_social_boost)
    ]
    ranked_for_social = sorted(
        near_threshold,
        key=lambda item: (
            max(0.0, item[2]["burst_ratio"] - 1.0),
            max(0.0, item[2]["ret_8"]),
            max(0.0, item[2]["last_ratio"] - 1.0),
            item[0].score,
        ),
        reverse=True,
    )[:social_slots]
    social_raw_by_symbol: dict[str, float] = {}
    social_reason_by_symbol: dict[str, str] = {}
    for rank, (candidate, _data_key, metrics, _is_new) in enumerate(ranked_for_social, start=1):
        raw_strength = (0.45 * max(0.0, metrics["burst_ratio"] - 1.0)) + (0.35 * max(0.0, metrics["ret_8"]) / 6.0) + (0.20 * max(0.0, metrics["last_ratio"] - 1.0))
        bounded_strength = max(0.0, min(1.0, raw_strength))
        raw_boost = round(max_social_boost * bounded_strength, 2)
        if raw_boost <= 0:
            continue
        social_raw_by_symbol[candidate.symbol] = raw_boost
        social_reason_by_symbol[candidate.symbol] = f"Backtest proxy buzz #{rank}: burst {metrics['burst_ratio']:.2f}x, ret {metrics['ret_8']:.2f}%"

    final_candidates: list[MoonshotProxyCandidate] = []
    for candidate, data_key, _metrics, is_new in re_scored:
        raw_boost = social_raw_by_symbol.get(candidate.symbol, 0.0)
        if raw_boost > 0:
            boost = _scale_social_buzz(candidate, raw_boost=raw_boost, threshold=score_threshold)
            if boost > 0:
                candidate.score = round(candidate.score + boost, 2)
                candidate.metadata["social_boost"] = round(_safe_float(candidate.metadata.get("social_boost")) + boost, 2)
                candidate.metadata["social_buzz"] = social_reason_by_symbol[candidate.symbol]
        if candidate.score < score_threshold:
            continue
        if candidate.entry_signal == "REBOUND_BURST" and not is_new:
            continue
        if is_new:
            candidate.score = round(candidate.score + 5.0, 2)
            candidate.metadata["recent_listing"] = True
        final_candidates.append(MoonshotProxyCandidate(opportunity=candidate, data_key=data_key))

    final_candidates.sort(key=lambda item: item.opportunity.score, reverse=True)
    return final_candidates