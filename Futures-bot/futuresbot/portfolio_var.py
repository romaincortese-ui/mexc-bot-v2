"""Sprint 3 §3.6 — cross-symbol portfolio VaR guard.

Pure module. Given a set of existing positions and a candidate new position,
plus a correlation matrix of 4h returns, compute the resulting portfolio
annualised volatility. Reject the candidate if it pushes portfolio vol above
the configured cap (default 8% annualised).

We keep the math deliberately simple — w.T @ Sigma @ w using notional-weight
vectors expressed as fractions of NAV. Sigma is expected pre-annualised
(sigma_i * sigma_j * rho_ij at annual scale).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

try:  # keep import optional so importing this module in tests is cheap
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class PositionWeight:
    symbol: str
    signed_notional_usdt: float  # +long, -short


@dataclass(frozen=True, slots=True)
class PortfolioVarCheck:
    portfolio_vol_annualised: float
    cap: float
    accepted: bool
    reason: str


def _vec_and_sigma(
    positions: list[PositionWeight],
    nav_usdt: float,
    annualised_vol: Mapping[str, float],
    correlation: Mapping[tuple[str, str], float],
) -> tuple[list[float], list[list[float]], list[str]]:
    if np is None:
        raise RuntimeError("numpy required for portfolio VaR math")
    symbols = [p.symbol for p in positions]
    weights = [p.signed_notional_usdt / max(nav_usdt, 1e-9) for p in positions]
    n = len(symbols)
    sigma = [[0.0] * n for _ in range(n)]
    for i, si in enumerate(symbols):
        sig_i = float(annualised_vol.get(si, 0.0))
        for j, sj in enumerate(symbols):
            sig_j = float(annualised_vol.get(sj, 0.0))
            if i == j:
                rho = 1.0
            else:
                rho = float(
                    correlation.get((si, sj), correlation.get((sj, si), 0.0))
                )
            sigma[i][j] = sig_i * sig_j * rho
    return weights, sigma, symbols


def portfolio_vol(
    *,
    positions: list[PositionWeight],
    nav_usdt: float,
    annualised_vol: Mapping[str, float],
    correlation: Mapping[tuple[str, str], float],
) -> float:
    """Compute portfolio annualised volatility (fraction of NAV)."""
    if np is None or not positions or nav_usdt <= 0:
        return 0.0
    weights, sigma, _ = _vec_and_sigma(positions, nav_usdt, annualised_vol, correlation)
    w = np.asarray(weights, dtype=float)
    s = np.asarray(sigma, dtype=float)
    variance = float(w @ s @ w)
    if variance < 0:
        variance = 0.0
    return float(variance ** 0.5)


def check_new_position(
    *,
    existing: list[PositionWeight],
    candidate: PositionWeight,
    nav_usdt: float,
    annualised_vol: Mapping[str, float],
    correlation: Mapping[tuple[str, str], float],
    cap_vol: float = 0.08,
) -> PortfolioVarCheck:
    """Return whether adding ``candidate`` keeps portfolio vol <= ``cap_vol``."""
    combined = list(existing) + [candidate]
    vol = portfolio_vol(
        positions=combined,
        nav_usdt=nav_usdt,
        annualised_vol=annualised_vol,
        correlation=correlation,
    )
    accepted = vol <= cap_vol
    reason = (
        f"portfolio_vol={vol:.4f} cap={cap_vol:.4f} -> "
        f"{'accept' if accepted else 'reject'}"
    )
    return PortfolioVarCheck(
        portfolio_vol_annualised=vol,
        cap=cap_vol,
        accepted=accepted,
        reason=reason,
    )
