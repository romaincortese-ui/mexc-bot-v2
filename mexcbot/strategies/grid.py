from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from mexcbot.config import LiveConfig
from mexcbot.config import env_float
from mexcbot.config import env_str
from mexcbot.exchange import MexcClient
from mexcbot.indicators import calc_adx, calc_atr, calc_bollinger_bands, calc_rsi
from mexcbot.models import Opportunity


log = logging.getLogger(__name__)

GRID_INTERVAL = env_str("GRID_INTERVAL", "15m")
GRID_BB_PERIOD = 20
GRID_BB_STD = 2.0
GRID_BB_WIDTH_MAX_PCT = 0.05
GRID_BB_WIDTH_SQUEEZE_PCT = 0.30
GRID_ADX_MAX = 18.0
GRID_RSI_MIN = 32.0
GRID_RSI_MAX = 52.0
GRID_ENTRY_BB_ZONE = 0.20
GRID_TP_BB_ZONE = 0.72
GRID_TP_MIN = 0.010
GRID_TP_MAX = 0.028
GRID_SL_MIN = 0.007
GRID_SL_CAP = env_float("GRID_SL_CAP", 0.40)
GRID_SL_MAX = 0.016
GRID_MIN_SCORE = 55.0
GRID_SPREAD_MAX = 0.002
GRID_UNIVERSE_MIN_ABS_CHANGE_PCT = 0.003
GRID_UNIVERSE_MAX_ABS_CHANGE_PCT = 0.08

GRID_EXCLUDED_BASE_ASSETS = {
    "EUR",
    "EURC",
    "FDUSD",
    "PAXG",
    "TUSD",
    "USDC",
    "USDE",
    "USDP",
    "USD1",
    "WBTC",
    "WETH",
    "XAUT",
}


def _grid_params() -> dict[str, float]:
    return {
        "bb_period": env_float("GRID_BB_PERIOD", GRID_BB_PERIOD),
        "bb_std": env_float("GRID_BB_STD", GRID_BB_STD),
        "bb_width_max_pct": env_float("GRID_BB_WIDTH_MAX_PCT", GRID_BB_WIDTH_MAX_PCT),
        "bb_width_squeeze_pct": env_float("GRID_BB_WIDTH_SQUEEZE_PCT", GRID_BB_WIDTH_SQUEEZE_PCT),
        "adx_max": env_float("GRID_ADX_MAX", GRID_ADX_MAX),
        "rsi_min": env_float("GRID_RSI_MIN", GRID_RSI_MIN),
        "rsi_max": env_float("GRID_RSI_MAX", GRID_RSI_MAX),
        "entry_bb_zone": env_float("GRID_ENTRY_BB_ZONE", GRID_ENTRY_BB_ZONE),
        "tp_bb_zone": env_float("GRID_TP_BB_ZONE", GRID_TP_BB_ZONE),
        "tp_min": env_float("GRID_TP_MIN", GRID_TP_MIN),
        "tp_max": env_float("GRID_TP_MAX", GRID_TP_MAX),
        "sl_min": env_float("GRID_SL_MIN", GRID_SL_MIN),
        "sl_max": env_float("GRID_SL_MAX", GRID_SL_MAX),
        "min_score": env_float("GRID_MIN_SCORE", GRID_MIN_SCORE),
        "spread_max": env_float("GRID_SPREAD_MAX", GRID_SPREAD_MAX),
        "universe_min_abs_change_pct": env_float("GRID_UNIVERSE_MIN_ABS_CHANGE_PCT", GRID_UNIVERSE_MIN_ABS_CHANGE_PCT),
        "universe_max_abs_change_pct": env_float("GRID_UNIVERSE_MAX_ABS_CHANGE_PCT", GRID_UNIVERSE_MAX_ABS_CHANGE_PCT),
    }


def _grid_base_asset(symbol: str) -> str:
    upper_symbol = symbol.upper()
    return upper_symbol[:-4] if upper_symbol.endswith("USDT") else upper_symbol


def _is_grid_tradeable_symbol(symbol: str) -> bool:
    base_asset = _grid_base_asset(symbol)
    if not base_asset or "(" in base_asset or ")" in base_asset:
        return False
    return base_asset not in GRID_EXCLUDED_BASE_ASSETS


def _build_grid_universe(tickers: pd.DataFrame, config: LiveConfig, params: dict[str, float]) -> list[str]:
    if tickers is None or tickers.empty:
        return []

    eligible = tickers[tickers["quoteVolume"] >= config.min_volume_usdt].copy()
    eligible = eligible[eligible["symbol"].map(_is_grid_tradeable_symbol)]
    if eligible.empty:
        return []

    eligible = eligible.assign(abs_change=eligible["priceChangePercent"].abs())
    min_abs_change = max(0.0, float(params["universe_min_abs_change_pct"]))
    max_abs_change = max(min_abs_change, float(params["universe_max_abs_change_pct"]))
    ranged = eligible[(eligible["abs_change"] >= min_abs_change) & (eligible["abs_change"] <= max_abs_change)]
    if ranged.empty:
        ranged = eligible

    ranked = ranged.sort_values(["quoteVolume", "abs_change"], ascending=[False, True])
    return ranked.head(config.universe_limit)["symbol"].tolist()


def score_grid_from_frame(symbol: str, frame: pd.DataFrame, score_threshold: float = GRID_MIN_SCORE) -> Opportunity | None:
    params = _grid_params()
    bb_period = int(params["bb_period"])
    if frame is None or len(frame) < bb_period + 5:
        return None

    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    opens = frame["open"].astype(float)
    price_now = float(close.iloc[-1])
    if price_now <= 0:
        return None

    upper, middle, lower, bb_width = calc_bollinger_bands(close, period=bb_period, std_mult=params["bb_std"])
    if np.isnan(upper) or np.isnan(lower) or np.isnan(bb_width) or middle <= 0:
        return None
    if bb_width > params["bb_width_max_pct"]:
        return None

    bb_hist: list[float] = []
    sma_hist = close.rolling(window=bb_period).mean()
    std_hist = close.rolling(window=bb_period).std()
    for index in range(bb_period, len(close)):
        mean_price = float(sma_hist.iloc[index])
        std_price = float(std_hist.iloc[index])
        if mean_price > 0 and not np.isnan(std_price):
            bb_hist.append((2 * params["bb_std"] * std_price) / mean_price)
    if len(bb_hist) < 10:
        return None
    width_percentile = sum(1 for width in bb_hist if width < bb_width) / len(bb_hist)
    if width_percentile > params["bb_width_squeeze_pct"]:
        return None

    adx = calc_adx(frame, period=14)
    if np.isnan(adx) or adx > params["adx_max"]:
        return None

    rsi_series = calc_rsi(close)
    current_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else float("nan")
    if np.isnan(current_rsi) or current_rsi < params["rsi_min"] or current_rsi > params["rsi_max"]:
        return None

    bb_range = upper - lower
    if bb_range <= 0:
        return None
    bb_position = (price_now - lower) / bb_range
    if bb_position > params["entry_bb_zone"]:
        return None

    avg_vol = float(volume.iloc[-20:-1].mean()) if len(volume) >= 21 else 0.0
    curr_vol = float(volume.iloc[-1])
    vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0.0
    if vol_ratio < 0.3:
        return None

    atr = calc_atr(frame, period=14)
    atr_pct = (atr / price_now) if not np.isnan(atr) and price_now > 0 else 0.01
    safe_opens = opens.replace(0, np.nan)
    raw_candle_pct = ((close - opens).abs() / safe_opens).iloc[-10:].mean()
    avg_candle_pct = float(raw_candle_pct) if not np.isnan(raw_candle_pct) else atr_pct

    score = 0.0
    score += max(0.0, (params["entry_bb_zone"] - bb_position) / params["entry_bb_zone"]) * 30.0
    score += max(0.0, (params["bb_width_squeeze_pct"] - width_percentile) / params["bb_width_squeeze_pct"]) * 25.0
    score += max(0.0, (params["adx_max"] - adx) / params["adx_max"]) * 20.0
    rsi_mid = (params["rsi_max"] + params["rsi_min"]) / 2.0
    if current_rsi < rsi_mid:
        score += max(0.0, (rsi_mid - current_rsi) / (rsi_mid - params["rsi_min"])) * 15.0
    vol_std = float(volume.iloc[-10:].std()) / avg_vol if avg_vol > 0 else 1.0
    if vol_std < 0.5:
        score += 10.0
    elif vol_std < 0.8:
        score += 5.0

    score = round(min(score, 100.0), 2)
    if score < max(score_threshold, params["min_score"]):
        return None

    tp_price_target = lower + bb_range * params["tp_bb_zone"]
    tp_pct = max(params["tp_min"], min(params["tp_max"], (tp_price_target / price_now) - 1))
    sl_price_target = lower * (1 - bb_width)
    sl_pct = min(GRID_SL_CAP, max(params["sl_min"], min(params["sl_max"], (price_now - sl_price_target) / price_now)))
    if sl_pct <= 0 or tp_pct / sl_pct < 1.5:
        return None

    return Opportunity(
        symbol=symbol,
        score=score,
        price=price_now,
        rsi=round(current_rsi, 2),
        rsi_score=0.0,
        ma_score=0.0,
        vol_score=round(max(0.0, min(10.0, vol_ratio * 4.0)), 2),
        vol_ratio=round(vol_ratio, 2),
        entry_signal="GRID_MEAN_REVERT",
        strategy="GRID",
        tp_pct=round(tp_pct, 6),
        sl_pct=round(sl_pct, 6),
        atr_pct=round(atr_pct, 6),
        metadata={
            "bb_position": round(bb_position, 4),
            "bb_width": round(bb_width, 6),
            "adx": round(adx, 2),
            "avg_candle_pct": round(avg_candle_pct, 6),
        },
    )


def find_grid_opportunity(
    client: MexcClient,
    config: LiveConfig,
    exclude: set[str] | None = None,
    open_symbols: set[str] | None = None,
) -> Opportunity | None:
    excluded = {symbol.upper() for symbol in (exclude or set())}
    excluded.update(symbol.upper() for symbol in (open_symbols or set()))
    params = _grid_params()

    symbols: list[str] = list(config.grid_symbols or [])
    if not symbols:
        try:
            tickers = client.get_all_tickers()
            symbols = _build_grid_universe(tickers, config, params)
            if symbols:
                log.debug("[GRID] dynamic universe=%s", ", ".join(symbols[:12]))
        except Exception as exc:
            log.debug("GRID dynamic universe failed: %s", exc)
            return None

    best: Opportunity | None = None
    for symbol in symbols:
        if symbol.upper() in excluded:
            continue
        try:
            frame = client.get_klines(symbol, interval=GRID_INTERVAL, limit=80)
            candidate = score_grid_from_frame(symbol, frame, score_threshold=params["min_score"])
            if candidate is None:
                continue
            try:
                depth = client.public_get("/api/v3/depth", {"symbol": symbol, "limit": 5})
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])
                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    mid = (best_bid + best_ask) / 2.0
                    spread = ((best_ask - best_bid) / mid) if mid > 0 else 1.0
                    if spread > params["spread_max"]:
                        continue
            except Exception:
                pass
            if best is None or candidate.score > best.score:
                best = candidate
        except Exception as exc:
            log.debug("GRID scoring failed for %s: %s", symbol, exc)
    return best