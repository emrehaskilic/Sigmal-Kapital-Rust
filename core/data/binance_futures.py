"""Binance Futures authenticated client — order placement, balance, positions.

Handles HMAC-SHA256 signing for private endpoints.
Uses only urllib (no extra dependencies) to match existing binance_rest.py style.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from typing import Any

import logging

logger = logging.getLogger("binance_futures")

_BASE = "https://fapi.binance.com"
_TESTNET_BASE = "https://testnet.binancefuture.com"


class BinanceFutures:
    """Authenticated Binance USDT-M Futures client."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base = _TESTNET_BASE if testnet else _BASE

    # ------------------------------------------------------------------
    # Signing & HTTP
    # ------------------------------------------------------------------

    def _sign(self, params: dict) -> str:
        """Create HMAC-SHA256 signature for request params."""
        query = urllib.parse.urlencode(params)
        sig = hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return sig

    def _request(
        self, method: str, path: str, params: dict | None = None, signed: bool = True,
        _max_retries: int = 3,
    ) -> Any:
        """Execute HTTP request with optional signing and retry on 429/5xx."""
        for attempt in range(_max_retries):
            req_params = dict(params or {})
            headers = {"X-MBX-APIKEY": self._api_key}

            if signed:
                req_params["timestamp"] = int(time.time() * 1000)
                req_params["signature"] = self._sign(req_params)

            url = f"{self._base}{path}"
            if method == "GET":
                qs = urllib.parse.urlencode(req_params)
                url = f"{url}?{qs}" if qs else url
                data = None
            else:
                data = urllib.parse.urlencode(req_params).encode()

            req = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                # Retry on 429 (rate limit) and 5xx (server error)
                if e.code in (429, 500, 502, 503) and attempt < _max_retries - 1:
                    wait = (attempt + 1) * 2  # 2s, 4s, 6s
                    logger.warning(
                        "Binance API %d: %s — retry %d/%d in %ds",
                        e.code, path, attempt + 1, _max_retries, wait,
                    )
                    time.sleep(wait)
                    continue
                logger.error("Binance API error %d: %s — %s", e.code, path, body)
                raise RuntimeError(f"Binance API {e.code}: {body}") from e

    # ------------------------------------------------------------------
    # Account Info
    # ------------------------------------------------------------------

    def get_balance(self) -> dict[str, float]:
        """Get futures account USDT balance.

        Returns: {balance, available, unrealized_pnl}
        """
        data = self._request("GET", "/fapi/v2/balance")
        for asset in data:
            if asset["asset"] == "USDT":
                return {
                    "balance": float(asset["balance"]),
                    "available": float(asset["availableBalance"]),
                    "unrealized_pnl": float(asset.get("crossUnPnl", 0)),
                }
        return {"balance": 0.0, "available": 0.0, "unrealized_pnl": 0.0}

    def get_positions(self) -> list[dict[str, Any]]:
        """Get all open positions (non-zero amount).

        Returns list of: {symbol, side, amount, entry_price, unrealized_pnl, leverage, margin_type}
        """
        data = self._request("GET", "/fapi/v2/positionRisk")
        positions = []
        for p in data:
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "amount": abs(amt),
                "entry_price": float(p["entryPrice"]),
                "mark_price": float(p.get("markPrice", 0)),
                "break_even_price": float(p.get("breakEvenPrice", p["entryPrice"])),
                "unrealized_pnl": float(p["unRealizedProfit"]),
                "leverage": int(p["leverage"]),
                "margin_type": p["marginType"],
                "notional": abs(float(p.get("notional", 0))),
                "isolated_margin": float(p.get("isolatedMargin", 0)),
            })
        return positions

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Get all open orders for a symbol."""
        data = self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        return data

    # ------------------------------------------------------------------
    # Leverage & Margin
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        return self._request("POST", "/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage,
        })

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        """Set margin type (ISOLATED or CROSSED). Ignores if already set."""
        try:
            return self._request("POST", "/fapi/v1/marginType", {
                "symbol": symbol,
                "marginType": margin_type,
            })
        except RuntimeError as e:
            # Binance returns error if margin type is already set — ignore
            if "No need to change margin type" in str(e):
                return {"msg": "already set"}
            raise

    # ------------------------------------------------------------------
    # Order Placement
    # ------------------------------------------------------------------

    def market_order(
        self, symbol: str, side: str, quantity: float, reduce_only: bool = False
    ) -> dict:
        """Place a market order.

        Args:
            symbol: e.g. "BTCUSDT"
            side: "BUY" or "SELL"
            quantity: base asset quantity
            reduce_only: True for closing positions
        """
        info = self.get_symbol_info(symbol)
        qty_precision = info.get("quantityPrecision", 3)
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{quantity:.{qty_precision}f}",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        result = self._request("POST", "/fapi/v1/order", params)
        logger.info(
            "[ORDER] MARKET %s %s qty=%.6f → orderId=%s status=%s",
            side, symbol, quantity, result.get("orderId"), result.get("status"),
        )
        return result

    def limit_order(
        self, symbol: str, side: str, quantity: float, price: float,
        reduce_only: bool = False
    ) -> dict:
        """Place a LIMIT order (for DCA/TP).

        Args:
            symbol: e.g. "BTCUSDT"
            side: "BUY" or "SELL"
            quantity: base asset quantity
            price: limit price
            reduce_only: True for TP (closing) orders
        """
        # Format as strings to avoid float precision issues (e.g. 1.7320000000000002)
        info = self.get_symbol_info(symbol)
        qty_precision = info.get("quantityPrecision", 3)
        price_precision = info.get("pricePrecision", 2)

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": f"{quantity:.{qty_precision}f}",
            "price": f"{price:.{price_precision}f}",
            "timeInForce": "GTC",
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        result = self._request("POST", "/fapi/v1/order", params)
        logger.info(
            "[ORDER] LIMIT %s %s qty=%.6f price=%.6f → orderId=%s status=%s",
            side, symbol, quantity, price, result.get("orderId"), result.get("status"),
        )
        return result

    def stop_market_order(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> dict:
        """Place a stop-market order (for SL).

        Args:
            side: "BUY" to close SHORT, "SELL" to close LONG
            stop_price: trigger price
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "quantity": quantity,
            "stopPrice": f"{stop_price}",
            "reduceOnly": "true",
        }
        result = self._request("POST", "/fapi/v1/order", params)
        logger.info(
            "[ORDER] STOP_MARKET %s %s qty=%.6f stop=%.4f → orderId=%s",
            side, symbol, quantity, stop_price, result.get("orderId"),
        )
        return result

    def take_profit_market_order(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> dict:
        """Place a take-profit-market order (for TP).

        Args:
            side: "BUY" to close SHORT, "SELL" to close LONG
            stop_price: trigger price
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "quantity": quantity,
            "stopPrice": f"{stop_price}",
            "reduceOnly": "true",
        }
        result = self._request("POST", "/fapi/v1/order", params)
        logger.info(
            "[ORDER] TP_MARKET %s %s qty=%.6f stop=%.4f → orderId=%s",
            side, symbol, quantity, stop_price, result.get("orderId"),
        )
        return result

    def get_order(self, symbol: str, order_id: int) -> dict:
        """Query a specific order to get fill details (avgPrice, status, executedQty)."""
        return self._request("GET", "/fapi/v1/order", {
            "symbol": symbol,
            "orderId": order_id,
        })

    def get_order_fill_price(self, symbol: str, order_id: int, max_retries: int = 3) -> float:
        """Poll order until FILLED and return avgPrice. Returns 0 if verification fails."""
        for attempt in range(max_retries):
            try:
                order = self.get_order(symbol, order_id)
                status = order.get("status", "")
                avg_price = float(order.get("avgPrice", 0))

                if status == "FILLED" and avg_price > 0:
                    return avg_price

                if status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.warning("[VERIFY] Order %s status=%s — not filled", order_id, status)
                    return 0.0

                # Still pending — wait briefly and retry
                if attempt < max_retries - 1:
                    import time as _time
                    _time.sleep(0.5)

            except Exception as e:
                logger.error("[VERIFY] Failed to query order %s: %s", order_id, e)

        logger.warning("[VERIFY] Order %s: could not confirm fill after %d retries", order_id, max_retries)
        return 0.0

    def cancel_all_orders(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        result = self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        logger.info("[ORDER] Cancel all orders for %s → %s", symbol, result)
        return result

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel a specific order."""
        return self._request("DELETE", "/fapi/v1/order", {
            "symbol": symbol,
            "orderId": order_id,
        })

    # ------------------------------------------------------------------
    # Exchange Info (for quantity precision)
    # ------------------------------------------------------------------

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def get_symbol_info(self, symbol: str) -> dict[str, Any]:
        """Get symbol trading rules (precision, min qty, etc.)."""
        if not hasattr(self, "_sym_cache"):
            self._sym_cache: dict[str, dict] = {}
            self._sym_cache_ts: float = 0
        now = time.time()
        if not self._sym_cache or now - self._sym_cache_ts > 3600:
            data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
            self._sym_cache = {}
            for s in data.get("symbols", []):
                self._sym_cache[s["symbol"]] = s
            self._sym_cache_ts = now
        return self._sym_cache.get(symbol, {})

    def calc_quantity(
        self, symbol: str, usdt_amount: float, leverage: int, price: float
    ) -> float:
        """Calculate order quantity from USDT margin, leverage, and price.

        Returns quantity rounded to symbol's precision.
        """
        import math
        notional = usdt_amount * leverage
        raw_qty = notional / price
        info = self.get_symbol_info(symbol)
        # Use stepSize from LOT_SIZE filter
        step_size = 0
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step_size = float(f.get("stepSize", 0))
                break
        if step_size > 0:
            qty = math.floor(raw_qty / step_size) * step_size
        else:
            precision = info.get("quantityPrecision", 3)
            qty = round(raw_qty, precision)
        # Ensure minimum notional (Binance requires >= 5 USDT notional)
        if qty * price < 5:
            return 0.0
        return qty

    def calc_price(self, symbol: str, price: float) -> float:
        """Round price to symbol's tick size (not just precision).

        Binance requires prices to be exact multiples of tickSize.
        """
        info = self.get_symbol_info(symbol)
        # Find tickSize from PRICE_FILTER
        tick_size = 0.0
        for f in info.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                tick_size = float(f.get("tickSize", 0))
                break
        if tick_size > 0:
            # Round down to nearest tick
            import math
            return math.floor(price / tick_size) * tick_size
        # Fallback to pricePrecision
        precision = info.get("pricePrecision", 2)
        return round(price, precision)

    def get_all_orders(self, symbol: str, limit: int = 100) -> list[dict]:
        """Get all orders (filled, cancelled, open) for a symbol.

        GET /fapi/v1/allOrders
        Returns most recent orders, sorted by time desc.
        """
        params = {"symbol": symbol, "limit": limit}
        return self._request("GET", "/fapi/v1/allOrders", params)

    # ------------------------------------------------------------------
    # User Data Stream — listenKey management
    # ------------------------------------------------------------------

    def create_listen_key(self) -> str:
        """Create a listenKey for User Data Stream (WS order updates).

        POST /fapi/v1/listenKey
        Returns the listenKey string. Valid for 60 minutes.
        Testnet/mainnet determined automatically by base_url.
        """
        data = self._request("POST", "/fapi/v1/listenKey", {}, signed=False)
        key = data.get("listenKey", "")
        logger.info("[UDS] Listen key created: %s...%s", key[:8], key[-4:])
        return key

    def renew_listen_key(self, listen_key: str) -> None:
        """Renew listenKey to extend validity by another 60 minutes.

        PUT /fapi/v1/listenKey — call every 25 minutes.
        """
        self._request(
            "PUT", "/fapi/v1/listenKey",
            {"listenKey": listen_key}, signed=False,
        )
        logger.debug("[UDS] Listen key renewed")

    def close_listen_key(self, listen_key: str) -> None:
        """Close listenKey and stop User Data Stream.

        DELETE /fapi/v1/listenKey — call on live stop.
        """
        try:
            self._request(
                "DELETE", "/fapi/v1/listenKey",
                {"listenKey": listen_key}, signed=False,
            )
            logger.info("[UDS] Listen key closed")
        except Exception as e:
            logger.error("[UDS] Failed to close listen key: %s", e)
