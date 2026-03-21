"""Binance Futures REST client — symbol listing and exchange info.

Provides both sync and async interfaces:
- Sync methods (fetch_*_sync) for Streamlit dashboard
- Async methods (fetch_*) for the async engine / WS pair manager
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
from typing import Any

_CACHE_TTL = 3600  # 1 hour
_BASE = "https://fapi.binance.com"
_TESTNET_BASE = "https://testnet.binancefuture.com"


class BinanceRest:
    """REST wrapper for Binance Futures with sync + async support."""

    def __init__(self, testnet: bool = False) -> None:
        self._symbols_cache: list[dict[str, Any]] = []
        self._cache_ts: float = 0.0
        self._base = _TESTNET_BASE if testnet else _BASE

    # ------------------------------------------------------------------
    # Sync helpers (for Streamlit)
    # ------------------------------------------------------------------

    @staticmethod
    def _sync_get(url: str, params: dict | None = None) -> Any:
        if params:
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "ScalperBot/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def fetch_futures_symbols_sync(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Return list of USDT-margined perpetual symbols (sync)."""
        now = time.time()
        if not force and self._symbols_cache and (now - self._cache_ts) < _CACHE_TTL:
            return self._symbols_cache

        data = self._sync_get(f"{self._base}/fapi/v1/exchangeInfo")

        symbols: list[dict[str, Any]] = []
        for s in data.get("symbols", []):
            if (
                s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
            ):
                symbols.append(
                    {
                        "symbol": s["symbol"],
                        "pricePrecision": s["pricePrecision"],
                        "quantityPrecision": s["quantityPrecision"],
                        "baseAsset": s["baseAsset"],
                        "quoteAsset": s["quoteAsset"],
                    }
                )

        symbols.sort(key=lambda x: x["symbol"])
        self._symbols_cache = symbols
        self._cache_ts = now
        return symbols

    def fetch_klines_sync(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Fetch historical kline/candlestick data (sync)."""
        params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        raw = self._sync_get(f"{self._base}/fapi/v1/klines", params)

        candles: list[dict[str, Any]] = []
        for k in raw:
            candles.append(
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": int(k[6]),
                }
            )
        return candles

    def fetch_book_tickers_sync(self, symbols: list[str] | None = None) -> dict[str, dict[str, float]]:
        """Fetch best bid/ask for symbols. Returns {SYMBOL: {bid, ask, bid_qty, ask_qty}}."""
        data = self._sync_get(f"{self._base}/fapi/v1/ticker/bookTicker")
        result: dict[str, dict[str, float]] = {}
        symbol_set = set(s.upper() for s in symbols) if symbols else None
        for t in data:
            sym = t["symbol"]
            if symbol_set and sym not in symbol_set:
                continue
            result[sym] = {
                "bid": float(t["bidPrice"]),
                "ask": float(t["askPrice"]),
                "bid_qty": float(t["bidQty"]),
                "ask_qty": float(t["askQty"]),
            }
        return result

    def fetch_ticker_24h_sync(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch 24h ticker stats (sync)."""
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        data = self._sync_get(f"{self._base}/fapi/v1/ticker/24hr", params or None)

        if isinstance(data, dict):
            data = [data]

        return [
            {
                "symbol": t["symbol"],
                "lastPrice": float(t["lastPrice"]),
                "priceChangePercent": float(t["priceChangePercent"]),
                "volume": float(t["volume"]),
                "quoteVolume": float(t["quoteVolume"]),
            }
            for t in data
        ]

    # ------------------------------------------------------------------
    # Async helpers (for WS engine / PairManager)
    # ------------------------------------------------------------------

    async def fetch_futures_symbols(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Async wrapper — runs sync in executor to avoid blocking."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.fetch_futures_symbols_sync(force=force))

    async def fetch_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Async wrapper."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.fetch_klines_sync(symbol, interval, limit))

    async def fetch_ticker_24h(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Async wrapper."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.fetch_ticker_24h_sync(symbol))

    async def fetch_book_tickers(self, symbols: list[str] | None = None) -> dict[str, dict[str, float]]:
        """Async wrapper."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.fetch_book_tickers_sync(symbols))

    async def close(self) -> None:
        """No-op for sync client, kept for interface compatibility."""
        pass
