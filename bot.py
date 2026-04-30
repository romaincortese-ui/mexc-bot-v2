"""Production entry point.

Sprint-1 memo feature flags default to ON for the live Railway deployment.
``os.environ.setdefault`` is used so operators can still force any flag OFF
from the Railway dashboard without redeploying — dashboard vars win.
"""
from __future__ import annotations

import os

_PROD_FLAG_DEFAULTS = {
    "USE_ATR_STOPS_V2": "1",              # §2.1 ATR-anchored stop-loss geometry
    "USE_FEE_NET_SCORE": "1",             # §2.2 Fee/slippage-adjusted score threshold
    "USE_STRATEGY_DEDUP": "1",            # §2.3 Winner-takes-all per symbol per bar
    "USE_CORRELATION_CAP": "1",           # §2.4 Correlation-aware sizing cap
    "USE_FEE_TIER_SIZING": "1",           # §2.6 Fee-tier aware sizing multiplier
    "USE_MOONSHOT_PER_SYMBOL_GATE": "1",  # §2.7 Per-symbol MOONSHOT hit-rate gate
    "USE_DRAWDOWN_KILL": "1",             # §2.8 Portfolio drawdown kill switch
    "USE_FIFO_TAX_LOTS": "1",             # §2.9 Shadow FIFO tax-lot ledger
    "USE_REVIEW_VALIDATION": "1",         # §2.10 Daily-review validation gate
    "USE_WEEKEND_FLATTEN": "1",           # §3.7 Weekend risk flatten
}
for _key, _value in _PROD_FLAG_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


def main() -> None:
    role = os.environ.get("MEXC_BOT_ROLE", "trading").strip().lower()
    if role in {"crypto_events", "crypto-events", "event_intelligence", "event-intelligence"}:
        from run_crypto_event_intelligence import main as run_crypto_event_intelligence

        run_crypto_event_intelligence()
        return

    from mexcbot.runtime import run_bot

    run_bot()


if __name__ == "__main__":
    main()
