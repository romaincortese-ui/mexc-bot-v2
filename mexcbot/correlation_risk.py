"""Correlation-adjusted portfolio risk cap (Spot Sprint 1 §2.4).

Replaces the additive per-trade Kelly cap with a correlation-aware aggregate:

    portfolio_risk = sqrt( w.T * Sigma * w )

A first-pass implementation groups symbols into correlation buckets (majors,
L1 alts, memecoins, DeFi) and applies a fixed inter-bucket correlation matrix.
A full 30d rolling matrix can be swapped in later behind the same API.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


# Default bucket assignment for the common MEXC USDT universe. Entries missing
# here fall back to "ALT" which is treated as high-beta to BTC.
DEFAULT_BUCKETS: dict[str, str] = {
    "BTCUSDT": "MAJOR",
    "ETHUSDT": "MAJOR",
    "BNBUSDT": "MAJOR",
    "SOLUSDT": "L1_ALT",
    "ADAUSDT": "L1_ALT",
    "AVAXUSDT": "L1_ALT",
    "DOTUSDT": "L1_ALT",
    "XRPUSDT": "L1_ALT",
    "ATOMUSDT": "L1_ALT",
    "NEARUSDT": "L1_ALT",
    "APTUSDT": "L1_ALT",
    "SUIUSDT": "L1_ALT",
    "DOGEUSDT": "MEME",
    "PEPEUSDT": "MEME",
    "WIFUSDT": "MEME",
    "BONKUSDT": "MEME",
    "FLOKIUSDT": "MEME",
    "SHIBUSDT": "MEME",
    "UNIUSDT": "DEFI",
    "AAVEUSDT": "DEFI",
    "MKRUSDT": "DEFI",
    "LINKUSDT": "DEFI",
    "ENAUSDT": "DEFI",
}

# Cross-bucket correlation used to build Sigma. Conservative (high) estimates
# chosen so netting is sparing — can be tightened once an empirical matrix
# replaces this block.
BUCKET_CORR: dict[tuple[str, str], float] = {
    ("MAJOR", "MAJOR"): 1.0,
    ("MAJOR", "L1_ALT"): 0.85,
    ("MAJOR", "MEME"): 0.70,
    ("MAJOR", "DEFI"): 0.80,
    ("L1_ALT", "L1_ALT"): 0.92,
    ("L1_ALT", "MEME"): 0.75,
    ("L1_ALT", "DEFI"): 0.85,
    ("MEME", "MEME"): 0.88,
    ("MEME", "DEFI"): 0.70,
    ("DEFI", "DEFI"): 0.90,
    ("ALT", "ALT"): 0.85,
    ("MAJOR", "ALT"): 0.80,
    ("L1_ALT", "ALT"): 0.85,
    ("MEME", "ALT"): 0.80,
    ("DEFI", "ALT"): 0.82,
}

DEFAULT_PORTFOLIO_RISK_CAP_PCT: float = 0.04  # 4% of equity (memo §2.4)


@dataclass(frozen=True, slots=True)
class PortfolioRiskAssessment:
    portfolio_risk_pct: float
    cap_pct: float
    would_breach: bool
    per_symbol_risk_pct: dict[str, float]


def bucket_for(symbol: str, overrides: dict[str, str] | None = None) -> str:
    sym = (symbol or "").strip().upper()
    if overrides and sym in overrides:
        return overrides[sym]
    return DEFAULT_BUCKETS.get(sym, "ALT")


def _rho(b1: str, b2: str) -> float:
    key = (b1, b2) if (b1, b2) in BUCKET_CORR else (b2, b1)
    if key in BUCKET_CORR:
        return BUCKET_CORR[key]
    # Unknown pair — assume high correlation (conservative for a risk gate).
    return 0.80


def compute_portfolio_risk(
    *,
    exposures_pct: dict[str, float],
    bucket_overrides: dict[str, str] | None = None,
) -> float:
    """Return ``sqrt(w.T * Sigma * w)`` using bucket-level correlations.

    ``exposures_pct`` maps ``symbol -> signed risk weight as fraction of
    equity`` (e.g. long 1.5% = 0.015; short = -0.015). Pure function.
    """

    if not exposures_pct:
        return 0.0
    items = [(sym, float(w)) for sym, w in exposures_pct.items() if w]
    if not items:
        return 0.0
    buckets = {sym: bucket_for(sym, bucket_overrides) for sym, _ in items}
    variance = 0.0
    for i, (si, wi) in enumerate(items):
        bi = buckets[si]
        for j, (sj, wj) in enumerate(items):
            bj = buckets[sj]
            rho = 1.0 if i == j else _rho(bi, bj)
            variance += wi * wj * rho
    return sqrt(max(0.0, variance))


def would_breach_cap(
    *,
    existing_exposures_pct: dict[str, float],
    new_symbol: str,
    new_risk_pct: float,
    cap_pct: float = DEFAULT_PORTFOLIO_RISK_CAP_PCT,
    bucket_overrides: dict[str, str] | None = None,
) -> PortfolioRiskAssessment:
    """Check whether adding ``new_symbol`` at ``new_risk_pct`` breaches ``cap_pct``."""

    trial = dict(existing_exposures_pct)
    sym = new_symbol.upper()
    trial[sym] = trial.get(sym, 0.0) + float(new_risk_pct)
    total = compute_portfolio_risk(exposures_pct=trial, bucket_overrides=bucket_overrides)
    return PortfolioRiskAssessment(
        portfolio_risk_pct=total,
        cap_pct=float(cap_pct),
        would_breach=total > float(cap_pct),
        per_symbol_risk_pct=trial,
    )
