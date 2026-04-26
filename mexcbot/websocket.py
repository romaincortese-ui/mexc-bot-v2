"""WebSocket price monitor for MEXC spot market.

Subscribes to miniTicker streams for open positions, providing
sub-second price updates instead of REST polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

log = logging.getLogger(__name__)

WS_URL = "wss://wbs-api.mexc.com/ws"
WS_PING_SECS = 20
WS_STALE_SECS = 60


class PriceMonitor:
    """Thread-safe WebSocket price cache for MEXC miniTicker."""

    def __init__(self) -> None:
        self._prices: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._wanted: set[str] = set()
        self._wanted_lock = threading.Lock()
        self._running = False

    # ── Public API ──────────────────────────────────────────────

    def start(self) -> None:
        """Start the WebSocket monitor in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-price-monitor",
        )
        self._thread.start()
        log.info("WebSocket price monitor starting...")

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self._running = False

    def get_price(self, symbol: str) -> float | None:
        """Return the latest WS price for *symbol*, or None if stale/missing."""
        with self._lock:
            entry = self._prices.get(symbol)
        if entry is None:
            return None
        price, updated_at = entry
        if time.time() - updated_at > WS_STALE_SECS:
            return None
        return price

    def set_symbols(self, symbols: set[str]) -> None:
        """Update the set of symbols to subscribe to."""
        with self._wanted_lock:
            self._wanted = set(symbols)

    @staticmethod
    def _is_clean_close(exc: Exception) -> bool:
        return type(exc).__name__ == "ConnectionClosedOK"

    @classmethod
    def _reconnect_delay(cls, exc: Exception, backoff: int) -> int:
        if cls._is_clean_close(exc):
            return 2
        return backoff

    @classmethod
    def _next_backoff(cls, exc: Exception, backoff: int) -> int:
        if cls._is_clean_close(exc):
            return 2
        return min(backoff * 2, 60)

    @classmethod
    def _log_reconnect(cls, exc: Exception, delay: int) -> None:
        if cls._is_clean_close(exc):
            log.debug(
                "WS closed cleanly (%s: %s) — reconnect in %ss",
                type(exc).__name__, exc, delay,
            )
            return
        log.warning(
            "WS error (%s: %s) — reconnect in %ss",
            type(exc).__name__, exc, delay,
        )

    # ── Internal ────────────────────────────────────────────────

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_loop())
        except Exception as exc:
            log.error("WS monitor thread crashed: %s", exc)
        finally:
            loop.close()

    async def _ws_loop(self) -> None:
        try:
            import websockets  # type: ignore[import-untyped]
        except ImportError:
            log.error("'websockets' library not installed — WS monitor disabled.")
            return

        backoff = 2
        last_wanted: set[str] = set()

        while self._running:
            with self._wanted_lock:
                wanted = set(self._wanted)
            if not wanted:
                await asyncio.sleep(5)
                continue

            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=None,
                    close_timeout=5,
                    open_timeout=10,
                ) as ws:
                    log.debug("WS price monitor connected")
                    backoff = 2
                    subscribed: set[str] = set()
                    last_ping = time.time()

                    while self._running:
                        with self._wanted_lock:
                            wanted = set(self._wanted)

                        # Subscribe to new symbols
                        if wanted != last_wanted:
                            new_subs = wanted - subscribed
                            if new_subs:
                                await ws.send(json.dumps({
                                    "method": "SUBSCRIPTION",
                                    "params": [
                                        f"spot@public.miniTicker.v3.api@{s}"
                                        for s in new_subs
                                    ],
                                }))
                                subscribed |= new_subs
                                log.info("WS subscribed: %s", sorted(new_subs))

                            old_subs = subscribed - wanted
                            if old_subs:
                                await ws.send(json.dumps({
                                    "method": "UNSUBSCRIPTION",
                                    "params": [
                                        f"spot@public.miniTicker.v3.api@{s}"
                                        for s in old_subs
                                    ],
                                }))
                                subscribed -= old_subs
                                log.debug("WS unsubscribed: %s", sorted(old_subs))

                            last_wanted = wanted

                        # Keep-alive ping
                        if time.time() - last_ping >= WS_PING_SECS:
                            await ws.send(json.dumps({"method": "PING"}))
                            last_ping = time.time()

                        # Receive price updates
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue

                        if isinstance(raw, bytes):
                            continue

                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        d = msg.get("d", {})
                        sym = msg.get("s") or d.get("s")
                        px = d.get("p")
                        if sym and px:
                            with self._lock:
                                self._prices[sym] = (float(px), time.time())

            except Exception as exc:
                if not self._running:
                    break
                delay = self._reconnect_delay(exc, backoff)
                self._log_reconnect(exc, delay)
                await asyncio.sleep(delay)
                backoff = self._next_backoff(exc, backoff)
