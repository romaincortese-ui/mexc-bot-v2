from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mexcbot.config import LiveConfig
from mexcbot.exchange import MexcClient


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def main() -> int:
    raw_key = os.getenv("MEXC_API_KEY", "")
    raw_secret = os.getenv("MEXC_API_SECRET", "")
    config = LiveConfig.from_env()
    client = MexcClient(config)

    result: dict[str, object] = {
        "base_url": config.base_url,
        "api_key": _mask(config.api_key),
        "api_key_length": len(config.api_key),
        "api_secret_length": len(config.api_secret),
        "raw_key_had_outer_whitespace": raw_key != raw_key.strip(),
        "raw_secret_had_outer_whitespace": raw_secret != raw_secret.strip(),
        "recv_window_ms": int(os.getenv("MEXC_RECV_WINDOW_MS", "5000") or "5000"),
        "paper_trade": config.paper_trade,
    }

    try:
        server_time = client.get_server_time()
        local_time = int(time.time() * 1000)
        result["server_time"] = server_time
        result["local_time"] = local_time
        result["server_time_offset_ms"] = server_time - local_time
    except Exception as exc:
        result["server_time_error"] = str(exc)

    try:
        account = client.private_get("/api/v3/account")
        balances = account.get("balances", []) if isinstance(account, dict) else []
        result["private_get_ok"] = True
        result["balance_count"] = len(balances)
        result["private_account_keys"] = sorted(account.keys()) if isinstance(account, dict) else []
    except Exception as exc:
        result["private_get_ok"] = False
        result["private_get_error"] = str(exc)
        result["request_diag"] = client.get_private_request_diagnostics("/api/v3/account")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("private_get_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())