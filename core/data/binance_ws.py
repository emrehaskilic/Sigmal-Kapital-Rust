"""Binance Futures WebSocket — combined kline + bookTicker streams with dynamic subscribe/unsubscribe."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

_WS_BASE = "wss://fstream.binance.com/ws"
_WS_TESTNET_BASE = "wss://stream.binancefuture.com/ws"

OnCandleCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
OnBookTickerCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
OnOrderUpdateCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class BinanceWS:
    """Manages a single WS connection with dynamic stream subscriptions."""

    def __init__(
        self,
        on_candle: OnCandleCallback,
        on_book_ticker: OnBookTickerCallback | None = None,
        testnet: bool = False,
    ) -> None:
        self._on_candle = on_candle
        self._on_book_ticker = on_book_ticker
        self._ws_base = _WS_TESTNET_BASE if testnet else _WS_BASE
        self._ws: ClientConnection | None = None
        self._subscriptions: set[str] = set()
        self._running = False
        self._req_id = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WS connection and start the read loop."""
        self._running = True
        asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _run_loop(self) -> None:
        """Auto-reconnect read loop."""
        while self._running:
            try:
                async with websockets.connect(
                    self._ws_base, ping_interval=10, ping_timeout=3,
                ) as ws:
                    self._ws = ws
                    # Re-subscribe on reconnect
                    if self._subscriptions:
                        await self._send_subscribe(list(self._subscriptions))
                    logger.info("WS connected to %s", self._ws_base)
                    async for raw_msg in ws:
                        await self._handle_message(raw_msg)
            except websockets.ConnectionClosed:
                logger.warning("WS connection closed, reconnecting in 1s...")
                await asyncio.sleep(1)
            except Exception:
                logger.exception("WS error, reconnecting in 2s...")
                await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    async def subscribe(self, symbol: str, interval: str = "15m") -> None:
        """Subscribe to a kline stream for the given symbol."""
        stream = f"{symbol.lower()}@kline_{interval}"
        async with self._lock:
            if stream in self._subscriptions:
                return
            self._subscriptions.add(stream)
            if self._ws:
                await self._send_subscribe([stream])
        logger.info("Subscribed: %s", stream)

    async def unsubscribe(self, symbol: str, interval: str = "15m") -> None:
        """Unsubscribe from a kline stream."""
        stream = f"{symbol.lower()}@kline_{interval}"
        async with self._lock:
            self._subscriptions.discard(stream)
            if self._ws:
                await self._send_unsubscribe([stream])
        logger.info("Unsubscribed: %s", stream)

    async def subscribe_book_ticker(self, symbol: str) -> None:
        """Subscribe to bookTicker stream for real-time bid/ask."""
        stream = f"{symbol.lower()}@bookTicker"
        async with self._lock:
            if stream in self._subscriptions:
                return
            self._subscriptions.add(stream)
            if self._ws:
                await self._send_subscribe([stream])
        logger.info("Subscribed bookTicker: %s", stream)

    async def unsubscribe_book_ticker(self, symbol: str) -> None:
        """Unsubscribe from bookTicker stream."""
        stream = f"{symbol.lower()}@bookTicker"
        async with self._lock:
            self._subscriptions.discard(stream)
            if self._ws:
                await self._send_unsubscribe([stream])
        logger.info("Unsubscribed bookTicker: %s", stream)

    async def _send_subscribe(self, streams: list[str]) -> None:
        self._req_id += 1
        msg = {"method": "SUBSCRIBE", "params": streams, "id": self._req_id}
        await self._ws.send(json.dumps(msg))

    async def _send_unsubscribe(self, streams: list[str]) -> None:
        self._req_id += 1
        msg = {"method": "UNSUBSCRIBE", "params": streams, "id": self._req_id}
        await self._ws.send(json.dumps(msg))

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, raw: str) -> None:
        data = json.loads(raw)

        # Skip subscription confirmations
        if "result" in data or "id" in data:
            return

        event_type = data.get("e")

        # Kline event
        if event_type == "kline":
            k = data["k"]
            candle = {
                "symbol": data["s"],
                "interval": k["i"],
                "open_time": k["t"],
                "close_time": k["T"],
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"]),
                "is_closed": k["x"],
            }
            await self._on_candle(candle)

        # BookTicker event — real-time best bid/ask
        elif event_type == "bookTicker" and self._on_book_ticker:
            ticker = {
                "symbol": data["s"],
                "bid": float(data["b"]),
                "ask": float(data["a"]),
                "bid_qty": float(data["B"]),
                "ask_qty": float(data["A"]),
                "time": data.get("T", 0),
            }
            await self._on_book_ticker(ticker)


class BinanceUserDataWS:
    """WebSocket for Binance User Data Stream — real-time order fill notifications.

    Separate from BinanceWS (market data) to keep concerns isolated.
    Handles: ORDER_TRADE_UPDATE, ACCOUNT_UPDATE events.
    Auto-reconnects on disconnect with catch-up poll callback.
    """

    def __init__(
        self,
        on_order_update: OnOrderUpdateCallback,
        on_reconnect: Callable[[], Coroutine[Any, Any, None]] | None = None,
        testnet: bool = False,
    ) -> None:
        self._on_order_update = on_order_update
        self._on_reconnect = on_reconnect
        self._ws_base = _WS_TESTNET_BASE if testnet else _WS_BASE
        self._ws: ClientConnection | None = None
        self._listen_key: str = ""
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, listen_key: str) -> None:
        """Start User Data Stream with the given listenKey."""
        self._listen_key = listen_key
        self._running = True
        asyncio.create_task(self._run_loop())

    async def reconnect(self, new_listen_key: str) -> None:
        """Reconnect with a new listenKey (for renewal or 24h reconnect)."""
        self._listen_key = new_listen_key
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        # _run_loop will auto-reconnect with new key

    async def close(self) -> None:
        self._running = False
        self._connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _run_loop(self) -> None:
        """Auto-reconnect read loop for User Data Stream."""
        while self._running:
            url = f"{self._ws_base}/{self._listen_key}"
            try:
                async with websockets.connect(
                    url, ping_interval=10, ping_timeout=3,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("[UDS_WS] Connected to User Data Stream")

                    # On reconnect, trigger catch-up poll
                    if self._on_reconnect:
                        try:
                            await self._on_reconnect()
                        except Exception as e:
                            logger.error("[UDS_WS] Reconnect callback error: %s", e)

                    async for raw_msg in ws:
                        await self._handle_message(raw_msg)

            except websockets.ConnectionClosed:
                self._connected = False
                logger.warning("[UDS_WS] Connection closed, reconnecting in 1s...")
                await asyncio.sleep(1)
            except Exception:
                self._connected = False
                logger.exception("[UDS_WS] Error, reconnecting in 2s...")
                await asyncio.sleep(2)

    async def _handle_message(self, raw: str) -> None:
        data = json.loads(raw)
        event_type = data.get("e")

        if event_type == "ORDER_TRADE_UPDATE":
            await self._on_order_update(data)

        elif event_type == "ACCOUNT_UPDATE":
            # Balance/position changes — log for observability
            logger.debug("[UDS_ACCOUNT] %s", json.dumps(data)[:200])

        elif event_type == "listenKeyExpired":
            logger.warning("[UDS_WS] Listen key expired! Forcing disconnect to trigger reconnect...")
            self._connected = False
            # Force close WS to trigger _run_loop's auto-reconnect
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass


class DualUserDataWS:
    """Redundant dual-connection UDS — two parallel BinanceUserDataWS instances.

    Both connections receive the same events. Dedup is handled downstream
    by LiveExecutor._processed_order_ids (already exists, zero extra code).
    If one connection drops, the other keeps delivering events.
    """

    def __init__(
        self,
        on_order_update: OnOrderUpdateCallback,
        on_reconnect: Callable[[], Coroutine[Any, Any, None]] | None = None,
        testnet: bool = False,
    ) -> None:
        self._a = BinanceUserDataWS(on_order_update, on_reconnect, testnet)
        self._b = BinanceUserDataWS(on_order_update, on_reconnect, testnet)

    @property
    def connected(self) -> bool:
        return self._a.connected or self._b.connected

    async def connect(self, listen_key: str) -> None:
        await self._a.connect(listen_key)
        await self._b.connect(listen_key)
        logger.info("[DUAL_UDS] Both connections started")

    async def reconnect(self, new_listen_key: str) -> None:
        await self._a.reconnect(new_listen_key)
        await self._b.reconnect(new_listen_key)
        logger.info("[DUAL_UDS] Both connections reconnecting with new key")

    async def close(self) -> None:
        await self._a.close()
        await self._b.close()
        logger.info("[DUAL_UDS] Both connections closed")
