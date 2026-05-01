"""§2.8 Session-aligned leverage caps for the futures bot.

Map UTC hour of day to a named session (ASIA / LONDON / US / OVERLAP) and a
leverage multiplier that reflects expected liquidity and volatility.

Pure module. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    session: str
    leverage_cap: int
    score_threshold_bump: float


def classify_session(hour_utc: int) -> str:
    h = int(hour_utc) % 24
    if 7 <= h < 13:
        return "LONDON"
    if 13 <= h < 16:
        return "OVERLAP"  # London + US overlap — deepest liquidity
    if 16 <= h < 21:
        return "US"
    return "ASIA"


def session_policy(
    hour_utc: int,
    *,
    full_leverage_cap: int = 10,
    asia_leverage_cap: int = 5,
    event_score_bump: float = 10.0,
    is_event_window: bool = False,
) -> SessionPolicy:
    """Return the session leverage cap and score-threshold bump for ``hour_utc``.

    ``is_event_window`` is a hint set by the caller when the current minute is
    within ±15min of a scheduled macro event (CPI, FOMC). During the US
    session this raises the score threshold by ``event_score_bump``.
    """

    session = classify_session(hour_utc)
    if session == "ASIA":
        cap = asia_leverage_cap
    else:
        cap = full_leverage_cap
    bump = 0.0
    if is_event_window and session in {"US", "OVERLAP"}:
        bump = max(0.0, event_score_bump)
    return SessionPolicy(session=session, leverage_cap=max(1, cap), score_threshold_bump=bump)
