"""FastAPI backend — REST API + WS status for the Scalper Bot dashboard."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from core.data.binance_rest import BinanceRest
from core.data.binance_ws import BinanceWS
from core.strategy.signals import SignalEngine
from core.engine.simulator import Simulator, Trade
from core.engine.backtester import Backtester
from core.engine.live_executor import LiveExecutor, PairConfig, PairState, LiveTrade
from core.data.binance_futures import BinanceFutures
from core.strategy.risk_manager import RiskManager

import pandas as pd
import asyncio
import json
import logging
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

app = FastAPI(title="Scalper Bot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global State ──
state: dict[str, Any] = {
    "config": load_config(),
    "rest": BinanceRest(),
    "simulator": None,
    "bot_running": False,
    "active_symbols": [],
    "scan_results": {},
    "signal_log": [],
    "ws_connected": False,
    "ws_last_ping": 0,
}

# ── WebSocket BookTicker — real-time bid/ask fed by Binance WS ──
_ws_book_data: dict[str, dict[str, float]] = {}  # {SYMBOL: {bid, ask, bid_qty, ask_qty, time}}
_ws_book_lock = threading.Lock()
_sim_lock = threading.Lock()  # protects simulator reads/writes across threads
_ws_instance: BinanceWS | None = None
_ws_loop: asyncio.AbstractEventLoop | None = None


async def _on_candle_noop(candle: dict) -> None:
    """Placeholder — kline handling not used in this WS instance."""
    pass


async def _on_book_ticker(ticker: dict) -> None:
    """Update in-memory book ticker cache from WS stream."""
    with _ws_book_lock:
        _ws_book_data[ticker["symbol"]] = ticker
    state["ws_connected"] = True
    state["ws_last_ping"] = time.time()


def _start_ws_loop(symbols: list[str]) -> None:
    """Start the WS event loop in a background thread."""
    global _ws_instance, _ws_loop

    loop = asyncio.new_event_loop()
    _ws_loop = loop

    async def _run():
        global _ws_instance
        ws = BinanceWS(on_candle=_on_candle_noop, on_book_ticker=_on_book_ticker)
        _ws_instance = ws
        await ws.connect()
        for sym in symbols:
            await ws.subscribe_book_ticker(sym)
        logger.info("WS bookTicker subscribed for %d symbols", len(symbols))
        # Keep loop alive
        while state["bot_running"]:
            await asyncio.sleep(1)
        try:
            await ws.close()
        except Exception:
            pass
        _ws_instance = None

    loop.run_until_complete(_run())
    loop.close()


def _stop_ws() -> None:
    """Signal WS loop to stop."""
    global _ws_instance, _ws_loop
    _ws_book_data.clear()
    _ws_instance = None
    _ws_loop = None


# ── Periodic Signal Scanner — detects new crossovers while bot is running ──

_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
}


def _signal_scanner_loop() -> None:
    """Background thread: re-check PMax crossovers for each TF on candle close."""
    logger.info("Signal scanner started")
    last_scan_buckets: dict[str, int] = {}  # tf_label → last bucket

    while state["bot_running"]:
        time.sleep(5)

        if not state["bot_running"] or not state["active_symbols"]:
            continue

        cfg = state["config"]
        tf_configs = cfg["strategy"].get("timeframes", [])
        if not tf_configs:
            continue

        sim = state["simulator"]
        if not sim:
            continue

        rest: BinanceRest = state["rest"]
        now = int(time.time())

        for tf_cfg in tf_configs:
            tf_label = tf_cfg.get("label", "1m")
            interval = tf_cfg.get("timeframe", "1m")
            interval_s = _TIMEFRAME_SECONDS.get(interval, 60)

            current_bucket = now // interval_s
            if current_bucket == last_scan_buckets.get(tf_label, 0):
                continue
            last_scan_buckets[tf_label] = current_bucket

            time.sleep(5)  # wait for candle to finalize

            logger.info("Signal scanner: new %s candle — rescanning %d symbols",
                         tf_label, len(state["active_symbols"]))

            for sym in list(state["active_symbols"]):
                if not state["bot_running"]:
                    break
                try:
                    klines = rest.fetch_klines_sync(sym, interval, limit=1500)
                    if len(klines) < 200:
                        continue

                    last_closed = klines[-2] if len(klines) >= 2 else klines[-1]
                    klines_for_signal = klines[:-1]

                    df = pd.DataFrame(klines_for_signal)
                    df["symbol"] = sym

                    engine = SignalEngine(cfg, tf_config=tf_cfg)
                    signal = engine.process(df)

                    # Keltner DCA/TP check — needs full DataFrame
                    with _sim_lock:
                        if sim.has_position(sym, tf_label):
                            exit_trades = sim.process_candle_with_df(
                                sym, df, tf_label=tf_label,
                            )
                            for t in exit_trades:
                                state["signal_log"].append({
                                    "time": time.strftime("%H:%M:%S"),
                                    "symbol": sym, "side": t.side,
                                    "price": t.exit_price, "rsi": 0,
                                    "source": f"{t.exit_reason} [{tf_label}]",
                                })

                    # PMax reversal = kill switch
                    if signal:
                        with _sim_lock:
                            pos_key = f"{sym}:{tf_label}"
                            has_pos = sim.has_position(sym, tf_label)
                            if has_pos:
                                existing = sim.positions.get(pos_key)
                                if existing and existing.side == signal.side:
                                    continue
                            reversal_trades = sim.process_signal(signal)
                            for rt in reversal_trades:
                                state["signal_log"].append({
                                    "time": time.strftime("%H:%M:%S"),
                                    "symbol": sym, "side": rt.side,
                                    "price": rt.exit_price, "rsi": 0,
                                    "source": f"REVERSAL [{tf_label}]",
                                })

                            pos = sim.positions.get(pos_key)
                            if pos and pos.condition != 0.0:
                                logger.info(
                                    "[ENTRY] %s %s [%s] @ %.4f",
                                    sym, signal.side, tf_label, signal.price,
                                )

                            state["signal_log"].append({
                                "time": time.strftime("%H:%M:%S"),
                                "symbol": sym, "side": signal.side,
                                "price": signal.price,
                                "rsi": round(signal.rsi_value, 2),
                                "source": f"LIVE_SCAN [{tf_label}]",
                            })
                            logger.info("Signal scanner: %s %s [%s] @ %.4f",
                                        sym, signal.side, tf_label, signal.price)

                except Exception as e:
                    logger.error("Signal scanner error for %s [%s]: %s",
                                 sym, tf_label, str(e)[:100])


def _get_sim() -> Simulator:
    if state["simulator"] is None:
        state["simulator"] = Simulator(state["config"])
    return state["simulator"]


# ── REST Endpoints ──

@app.get("/api/symbols")
def get_symbols():
    """Return all Binance Futures USDT-M perpetual symbols."""
    rest: BinanceRest = state["rest"]
    symbols = rest.fetch_futures_symbols_sync()
    return {"symbols": [s["symbol"] for s in symbols], "count": len(symbols)}


@app.get("/api/config")
def get_config():
    return state["config"]


@app.post("/api/config")
def update_config(body: dict):
    """Update config from frontend."""
    cfg = state["config"]
    if "trading" in body:
        cfg["trading"].update(body["trading"])
    if "strategy" in body:
        cfg["strategy"].update(body["strategy"])
    if "risk" in body:
        cfg["risk"].update(body["risk"])
    # Reset simulator with new config
    state["simulator"] = Simulator(cfg)
    return {"status": "ok"}


@app.post("/api/bot/start")
def start_bot(body: dict):
    """Start bot: run initial scan on selected pairs."""
    symbols = body.get("symbols", [])
    if not symbols:
        return {"error": "No symbols provided"}

    state["active_symbols"] = symbols
    state["bot_running"] = True
    state["ws_connected"] = True
    state["ws_last_ping"] = time.time()

    # Reset simulator
    cfg = state["config"]
    state["simulator"] = Simulator(cfg)
    sim = _get_sim()

    rest: BinanceRest = state["rest"]
    scan_results = {}
    signal_log = []

    # Get timeframe configs
    tf_configs = cfg["strategy"].get("timeframes", [])
    if not tf_configs:
        tf_configs = [{"label": "1m", "timeframe": "1m", "size_multiplier": 1,
                       "pmax": cfg["strategy"].get("pmax", {}),
                       "filters": cfg["strategy"].get("filters", {}),
                       "risk": cfg.get("risk", {})}]

    for sym in symbols:
        scan_results[sym] = {"status": "monitoring", "last_price": 0, "timeframes": {}}
        try:
            for tf_cfg in tf_configs:
                tf_label = tf_cfg.get("label", "1m")
                interval = tf_cfg.get("timeframe", "1m")

                klines = rest.fetch_klines_sync(sym, interval, limit=1500)
                if len(klines) > 1:
                    klines = klines[:-1]
                if len(klines) < 200:
                    scan_results[sym]["timeframes"][tf_label] = {
                        "status": "insufficient_data", "candles": len(klines),
                    }
                    continue

                df = pd.DataFrame(klines)
                df["symbol"] = sym

                engine = SignalEngine(cfg, tf_config=tf_cfg)
                # Only use backfill — show the last completed signal state
                # Do NOT use process() to avoid triggering new reversals on start
                signal = engine.process_backfill(df)

                last_price = float(df["close"].iloc[-1])
                scan_results[sym]["last_price"] = last_price

                if signal:
                    # Open position from backfill with ATR grid
                    sim.process_signal(signal)

                    # Replay candles from entry — use full DataFrame for ALMA
                    entry_ts = signal.timestamp
                    entry_idx = df[df["open_time"] > entry_ts].index
                    for idx in entry_idx:
                        if not sim.has_position(sym, tf_label):
                            break
                        # Slice DataFrame up to this candle for ALMA calculation
                        df_slice = df.iloc[:idx + 1]
                        exit_trades = sim.process_candle_with_df(
                            sym, df_slice, tf_label=tf_label,
                        )
                        for t in exit_trades:
                            signal_log.append({
                                "time": time.strftime("%H:%M:%S"),
                                "symbol": sym, "side": t.side,
                                "price": t.exit_price, "rsi": 0,
                                "source": f"{t.exit_reason} [{tf_label}]",
                            })

                    signal_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "symbol": sym, "side": signal.side,
                        "price": signal.price,
                        "rsi": round(signal.rsi_value, 2),
                        "source": f"INITIAL_SCAN [{tf_label}]",
                    })

                    pos_key = f"{sym}:{tf_label}"
                    pos = sim.positions.get(pos_key)
                    if pos and pos.condition != 0.0:
                        logger.info(
                            "[INIT_ENTRY] %s %s [%s] @ %.4f",
                            sym, signal.side, tf_label, signal.price,
                        )

                    if sim.has_position(sym, tf_label):
                        scan_results[sym]["status"] = "signal"
                        scan_results[sym]["timeframes"][tf_label] = {
                            "status": "signal", "side": signal.side,
                            "price": signal.price,
                            "rsi": round(signal.rsi_value, 2),
                        }
                    else:
                        scan_results[sym]["timeframes"][tf_label] = {
                            "status": "closed_tp", "side": signal.side,
                        }
                else:
                    from core.strategy.indicators import pmax as calc_pmax, rsi as calc_rsi
                    pmax_cfg = tf_cfg.get("pmax", {})
                    src_type = pmax_cfg.get("source", "hl2").lower()
                    if src_type == "hl2":
                        src = (df["high"] + df["low"]) / 2
                    elif src_type == "hlc3":
                        src = (df["high"] + df["low"] + df["close"]) / 3
                    elif src_type == "ohlc4":
                        src = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
                    else:
                        src = df["close"]
                    _, mavg, direction = calc_pmax(
                        src, df["high"], df["low"], df["close"],
                        atr_period=pmax_cfg.get("atr_period", 10),
                        atr_multiplier=pmax_cfg.get("atr_multiplier", 3.0),
                        ma_type=pmax_cfg.get("ma_type", "EMA"),
                        ma_length=pmax_cfg.get("ma_length", 10),
                        change_atr=pmax_cfg.get("change_atr", True),
                        normalize_atr=pmax_cfg.get("normalize_atr", False),
                    )
                    trend = "BULLISH" if direction.iloc[-1] == 1 else "BEARISH"
                    rsi_val = calc_rsi(df["close"], 28).iloc[-1]
                    scan_results[sym]["timeframes"][tf_label] = {
                        "status": "monitoring", "trend": trend,
                        "rsi": round(float(rsi_val), 2),
                    }
        except Exception as e:
            scan_results[sym] = {"status": "error", "message": str(e)[:100], "last_price": 0}

    state["scan_results"] = scan_results
    state["signal_log"] = signal_log

    # Start WS bookTicker stream in background thread
    _ws_book_data.clear()
    ws_thread = threading.Thread(target=_start_ws_loop, args=(symbols,), daemon=True)
    ws_thread.start()

    # Start periodic signal scanner in background thread
    scanner_thread = threading.Thread(target=_signal_scanner_loop, daemon=True)
    scanner_thread.start()

    return {
        "status": "started",
        "pairs": len(symbols),
        "immediate_signals": len(signal_log),
        "scan_results": scan_results,
    }


@app.post("/api/bot/stop")
def stop_bot():
    state["bot_running"] = False
    state["ws_connected"] = False
    state["active_symbols"] = []
    state["scan_results"] = {}
    state["simulator"] = None
    state["signal_log"] = []
    _stop_ws()
    return {"status": "stopped"}


# Orderbook: WS-fed real-time data with REST fallback
_rest_orderbook_cache: dict[str, Any] = {"data": {}, "ts": 0}
_REST_ORDERBOOK_CACHE_TTL = 5  # seconds — only used as fallback now


def _get_orderbook(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Get live bid/ask — prefers WS bookTicker, falls back to REST."""
    # Try WS data first (real-time, no latency)
    with _ws_book_lock:
        ws_data = {sym: _ws_book_data[sym] for sym in symbols if sym in _ws_book_data}

    if len(ws_data) == len(symbols):
        return ws_data

    # Partial or no WS data — fill gaps from REST
    now = time.time()
    missing = [s for s in symbols if s not in ws_data]
    if missing:
        if now - _rest_orderbook_cache["ts"] >= _REST_ORDERBOOK_CACHE_TTL or not _rest_orderbook_cache["data"]:
            try:
                rest: BinanceRest = state["rest"]
                book = rest.fetch_book_tickers_sync(symbols)
                _rest_orderbook_cache["data"] = book
                _rest_orderbook_cache["ts"] = now
            except Exception as e:
                logger.error("REST orderbook fallback failed: %s", e)

        rest_data = _rest_orderbook_cache.get("data", {})
        for sym in missing:
            if sym in rest_data:
                ws_data[sym] = rest_data[sym]

    return ws_data


def _mark_price_from_book(book_entry: dict[str, float], side: str) -> float:
    """Realistic mark price: LONG uses ask (you buy at ask), SHORT uses bid (you sell at bid)."""
    if side == "LONG":
        return book_entry["ask"]
    else:
        return book_entry["bid"]


@app.get("/api/status")
def get_status():
    """Main polling endpoint — returns full dashboard state with LIVE prices."""
    sim = state["simulator"]
    cfg = state["config"]

    # Wallet / stats
    stats = sim.get_stats() if sim else {
        "initial_balance": cfg["trading"]["initial_balance"],
        "current_balance": cfg["trading"]["initial_balance"],
        "total_pnl": 0, "total_pnl_pct": 0,
        "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
        "win_rate": 0, "total_fees": 0, "leverage": cfg["trading"]["leverage"],
    }

    # Fetch LIVE orderbook (bid/ask) — WS bookTicker preferred, REST fallback
    orderbook = {}
    live_prices = {}
    ws_symbols_count = 0
    if state["bot_running"] and state["active_symbols"]:
        orderbook = _get_orderbook(state["active_symbols"])
        # Count how many symbols come from WS vs REST
        with _ws_book_lock:
            ws_symbols_count = sum(1 for s in state["active_symbols"] if s in _ws_book_data)
        # Derive mid prices for backward compat
        for sym, ob in orderbook.items():
            live_prices[sym] = (ob["bid"] + ob["ask"]) / 2
        state["ws_connected"] = len(orderbook) > 0
        state["ws_last_ping"] = time.time()

    # Positions with LIVE PnL breakdown
    positions = []
    margin = cfg["trading"]["margin_per_trade"]
    leverage = cfg["trading"]["leverage"]
    maker_fee = cfg["trading"].get("maker_fee", cfg["trading"].get("fee_rate", 0.0002))
    taker_fee = cfg["trading"].get("taker_fee", cfg["trading"].get("fee_rate", 0.0005))

    if sim:
        # Lock ensures scanner thread can't mutate positions mid-read
        with _sim_lock:
            position_snapshot = [(pos_key, pos) for pos_key, pos in sim.positions.items()
                                 if pos.condition != 0.0]
        for pos_key, pos in position_snapshot:
            # Extract symbol and tf_label from key "BTCUSDT:1m"
            symbol_raw = pos.symbol  # pure symbol e.g. "BTCUSDT"
            tf_label = ""
            if ":" in pos_key:
                tf_label = pos_key.rsplit(":", 1)[1]

            # Size multiplier — Rust engine manages internally, default 1.0
            size_mult = 1.0

            # LIVE mark price from orderbook (key is pure symbol)
            ob = orderbook.get(symbol_raw)
            if ob:
                mark_price = _mark_price_from_book(ob, pos.side)
                bid = ob["bid"]
                ask = ob["ask"]
                spread = ask - bid
            else:
                mark_price = state["scan_results"].get(symbol_raw, {}).get("last_price", pos.entry_price)
                bid = mark_price
                ask = mark_price
                spread = 0.0

            # Notional from actual position state (includes DCA fills)
            position_notional = pos.total_position_notional if pos.total_position_notional > 0 else margin * size_mult * leverage

            # Unrealized PnL (LIVE)
            if pos.side == "LONG":
                upnl_pct = (mark_price - pos.entry_price) / pos.entry_price * 100
            else:
                upnl_pct = (pos.entry_price - mark_price) / pos.entry_price * 100
            upnl_usdt = position_notional * upnl_pct / 100

            # Break-even price
            total_fee_pct = taker_fee + maker_fee
            if pos.side == "LONG":
                break_even = pos.entry_price * (1 + total_fee_pct)
            else:
                break_even = pos.entry_price * (1 - total_fee_pct)

            # Realized PnL — match by pos_key via tf_label
            realized = sum(t.pnl_usdt for t in sim.trades
                           if t.symbol == symbol_raw and t.tf_label == tf_label)
            realized_fees = sum(t.fee_usdt for t in sim.trades
                                if t.symbol == symbol_raw and t.tf_label == tf_label)

            # Entry fee
            entry_fee = position_notional * taker_fee
            total_fees_for_pos = entry_fee + realized_fees

            positions.append({
                "symbol": pos_key,  # "BTCUSDT:1m" — so frontend shows TF
                "side": pos.side,
                "entry_price": pos.entry_price,
                "mark_price": round(mark_price, 4),
                "bid": round(bid, 4),
                "ask": round(ask, 4),
                "spread": round(spread, 6),
                "break_even": round(break_even, 4),
                "notional_usdt": round(position_notional, 2),
                "condition": pos.condition,
                "remaining_qty": pos.remaining_qty,
                "unrealized_pnl_usdt": round(upnl_usdt, 4),
                "unrealized_pnl_pct": round(upnl_pct, 4),
                "realized_pnl_usdt": round(realized, 4),
                "total_pnl_usdt": round(upnl_usdt + realized, 4),
                "fees_usdt": round(total_fees_for_pos, 4),
                "grid": None,  # grid levels shown via chart-data endpoint
            })

    # Refresh stats after possible TP/SL exits
    if sim:
        stats = sim.get_stats()

    # Fee breakdown
    fee_breakdown = {
        "maker": round(stats.get("maker_fees", 0), 4),
        "taker": round(stats.get("taker_fees", 0), 4),
        "total": round(stats.get("total_fees", 0), 4),
    }

    # Per-pair summary with LIVE prices
    pair_summaries = {}
    for sym in state["active_symbols"]:
        sym_trades = [t for t in (sim.trades if sim else []) if t.symbol == sym]
        sym_realized = sum(t.pnl_usdt for t in sym_trades)
        sym_fees = sum(t.fee_usdt for t in sym_trades)

        # Sum unrealized PnL across all TFs for this symbol
        sym_unrealized = sum(
            p["unrealized_pnl_usdt"] for p in positions
            if p["symbol"].startswith(sym + ":") or p["symbol"] == sym
        )
        current_price = live_prices.get(sym, state["scan_results"].get(sym, {}).get("last_price", 0))

        ob = orderbook.get(sym, {})
        scan = state["scan_results"].get(sym, {})
        pair_summaries[sym] = {
            "last_price": round(current_price, 4),
            "bid": round(ob.get("bid", current_price), 4),
            "ask": round(ob.get("ask", current_price), 4),
            "spread": round(ob.get("ask", 0) - ob.get("bid", 0), 6) if ob else 0,
            "status": scan.get("status", "waiting"),
            "trend": scan.get("trend", ""),
            "side": scan.get("side", ""),
            "rsi": scan.get("rsi", 0),
            "unrealized_pnl": round(sym_unrealized, 4),
            "realized_pnl": round(sym_realized, 4),
            "total_pnl": round(sym_unrealized + sym_realized, 4),
            "fees": round(sym_fees, 4),
            "trade_count": len(sym_trades),
        }

    # Total unrealized
    total_unrealized = sum(p["unrealized_pnl_usdt"] for p in positions)
    total_realized = stats["total_pnl"]

    return {
        "bot_running": state["bot_running"],
        "ws_connected": state["ws_connected"],
        "ws_last_ping": state["ws_last_ping"],
        "price_source": "websocket" if ws_symbols_count == len(state["active_symbols"]) else (
            f"mixed (ws:{ws_symbols_count}/rest:{len(state['active_symbols']) - ws_symbols_count})"
            if ws_symbols_count > 0 else "rest"
        ) if state["bot_running"] else "none",
        "active_symbols": state["active_symbols"],
        "stats": stats,
        "positions": positions,
        "pair_summaries": pair_summaries,
        "fees": fee_breakdown,
        "signal_log": state["signal_log"][-50:],  # last 50
        "trade_log": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "pnl_usdt": t.pnl_usdt,
                "pnl_pct": t.pnl_percent,
                "fee_usdt": t.fee_usdt,
                "leverage": t.leverage,
            }
            for t in (sim.trades if sim else [])
        ],
        "totals": {
            "unrealized_pnl": round(total_unrealized, 4),
            "realized_pnl": round(total_realized, 4),
            "total_pnl": round(total_unrealized + total_realized, 4),
            "total_fees": fee_breakdown["total"],
            "net_pnl": round(total_unrealized + total_realized - fee_breakdown["total"], 4),
        },
    }


# ══════════════════════════════════════════════════════════════════════
# CHART DATA ENDPOINT — OHLC + PMax + trade markers
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/chart-data")
def get_chart_data(symbol: str = "BTCUSDT", limit: int = 500):
    """Return OHLC candles + PMax indicator + trade markers for charting."""
    cfg = state["config"]
    rest: BinanceRest = state["rest"]

    tf_configs = cfg["strategy"].get("timeframes", [])
    if not tf_configs:
        return {"error": "No timeframes configured"}
    tf_cfg = tf_configs[0]
    interval = tf_cfg.get("timeframe", "3m")

    # Fetch klines
    try:
        klines = rest.fetch_klines_sync(symbol, interval, limit=limit)
    except Exception as e:
        return {"error": str(e)}

    if len(klines) < 50:
        return {"error": "Insufficient data"}

    df = pd.DataFrame(klines)
    df["symbol"] = symbol

    # Compute PMax (adaptive or static)
    from core.strategy.indicators import pmax as calc_pmax, adaptive_pmax as calc_adaptive_pmax
    pmax_cfg = tf_cfg.get("pmax", {})
    src_type = pmax_cfg.get("source", "hl2").lower()
    if src_type == "hl2":
        src = (df["high"] + df["low"]) / 2
    elif src_type == "hlc3":
        src = (df["high"] + df["low"] + df["close"]) / 3
    elif src_type == "ohlc4":
        src = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    else:
        src = df["close"]

    if pmax_cfg.get("adaptive", False):
        pmax_line, mavg, direction = calc_adaptive_pmax(
            src, df["high"], df["low"], df["close"], pmax_cfg,
        )
    else:
        pmax_line, mavg, direction = calc_pmax(
            src, df["high"], df["low"], df["close"],
            atr_period=pmax_cfg.get("atr_period", 10),
            atr_multiplier=pmax_cfg.get("atr_multiplier", 3.0),
            ma_type=pmax_cfg.get("ma_type", "EMA"),
            ma_length=pmax_cfg.get("ma_length", 10),
            change_atr=pmax_cfg.get("change_atr", True),
            normalize_atr=pmax_cfg.get("normalize_atr", False),
        )

    # Compute Keltner Channel
    from core.strategy.indicators import keltner_channel as calc_kc
    kc_cfg = tf_cfg.get("keltner", {})
    kc_mid, kc_upper_line, kc_lower_line = calc_kc(
        df["high"], df["low"], df["close"],
        kc_length=kc_cfg.get("length", 20),
        kc_multiplier=kc_cfg.get("multiplier", 1.5),
        atr_period=kc_cfg.get("atr_period", 10),
    )

    # Build candle data
    candles = []
    for i, row in df.iterrows():
        t = int(row["open_time"]) // 1000
        pmax_val = float(pmax_line.iloc[i]) if not pd.isna(pmax_line.iloc[i]) else None
        kc_upper_val = float(kc_upper_line.iloc[i]) if not pd.isna(kc_upper_line.iloc[i]) else None
        kc_lower_val = float(kc_lower_line.iloc[i]) if not pd.isna(kc_lower_line.iloc[i]) else None
        mavg_val = float(kc_mid.iloc[i]) if not pd.isna(kc_mid.iloc[i]) else None
        dir_val = int(direction.iloc[i]) if not pd.isna(direction.iloc[i]) else 0
        candles.append({
            "time": t,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "pmax": pmax_val,
            "mavg": mavg_val,
            "kc_upper": kc_upper_val,
            "kc_lower": kc_lower_val,
            "direction": dir_val,
        })

    # Build trade markers from simulator
    markers = []
    grid_levels = []
    pos = None
    sim = state.get("simulator")
    if sim:
        for trade in sim.trades:
            if trade.symbol != symbol:
                continue
            t = trade.entry_time // 1000 if trade.entry_time > 0 else 0
            exit_t = trade.exit_time // 1000 if trade.exit_time > 0 else 0

            reason = trade.exit_reason
            if reason.startswith("DCA"):
                # DCA entry marker (DCA1, DCA2, DCA3, DCA4)
                markers.append({
                    "time": exit_t or t,
                    "position": "belowBar" if trade.side == "LONG" else "aboveBar",
                    "color": "#22c55e" if trade.side == "LONG" else "#ef4444",
                    "shape": "arrowUp" if trade.side == "LONG" else "arrowDown",
                    "text": reason,
                    "price": trade.entry_price,
                })
            elif reason == "TP":
                # Take Profit marker
                markers.append({
                    "time": exit_t,
                    "position": "aboveBar" if trade.side == "LONG" else "belowBar",
                    "color": "#3b82f6",
                    "shape": "circle",
                    "text": f"TP +{trade.pnl_usdt:.2f}",
                    "price": trade.exit_price,
                })
            elif reason in ("REVERSAL", "REVERSAL_CLOSE"):
                # Reversal (kill switch) marker
                markers.append({
                    "time": exit_t,
                    "position": "aboveBar",
                    "color": "#f59e0b",
                    "shape": "square",
                    "text": f"REV {trade.pnl_usdt:+.2f}",
                    "price": trade.exit_price,
                })
            elif reason == "PCT_STOP":
                # Percentage hard stop marker (DCA full sonrasi %2.0 stop)
                markers.append({
                    "time": exit_t,
                    "position": "aboveBar" if trade.side == "LONG" else "belowBar",
                    "color": "#dc2626",
                    "shape": "square",
                    "text": f"STOP {trade.pnl_usdt:+.2f}",
                    "price": trade.exit_price,
                })
            elif reason == "DYN_SL":
                # Dynamic SL marker
                markers.append({
                    "time": exit_t,
                    "position": "aboveBar" if trade.side == "LONG" else "belowBar",
                    "color": "#dc2626",
                    "shape": "square",
                    "text": f"SL {trade.pnl_usdt:+.2f}",
                    "price": trade.exit_price,
                })
            elif reason == "HARD_STOP":
                # Hard stop marker
                markers.append({
                    "time": exit_t,
                    "position": "aboveBar" if trade.side == "LONG" else "belowBar",
                    "color": "#dc2626",
                    "shape": "square",
                    "text": f"HARD {trade.pnl_usdt:+.2f}",
                    "price": trade.exit_price,
                })

        # Add initial entry marker for open positions
        tf_label = tf_cfg.get("label", "3m")
        pos_key = f"{symbol}:{tf_label}"
        pos = sim.positions.get(pos_key)
        if pos and pos.condition != 0.0:
            entry_t = pos.entry_time // 1000 if pos.entry_time > 0 else 0
            markers.append({
                "time": entry_t,
                "position": "belowBar" if pos.side == "LONG" else "aboveBar",
                "color": "#22c55e" if pos.side == "LONG" else "#ef4444",
                "shape": "arrowUp" if pos.side == "LONG" else "arrowDown",
                "text": f"ENTRY {pos.side}",
                "price": pos.initial_entry_price,
            })

            # Grid levels: KC-based pending DCA/TP + avg entry + stop level
            # Average entry price line
            grid_levels.append({
                "price": pos.average_entry_price,
                "label": f"AVG ({pos.dca_fills_count} DCA)",
                "filled": True,
            })
            # Pending DCA level (from KC band)
            if pos.pending_dca_price > 0:
                max_dca = cfg["trading"].get("max_dca_steps", 4)
                if pos.dca_fills_count < max_dca:
                    grid_levels.append({
                        "price": pos.pending_dca_price,
                        "label": f"DCA{pos.dca_fills_count + 1}",
                        "filled": False,
                    })
            # Pending TP level (from KC band)
            if pos.pending_tp_price > 0 and pos.dca_fills_count > 0:
                grid_levels.append({
                    "price": pos.pending_tp_price,
                    "label": "TP",
                    "filled": False,
                })
            # PCT hard stop level (if DCA full)
            pct_cfg = cfg["trading"].get("pct_hard_stop", {})
            if pct_cfg.get("enabled", False):
                loss_pct = pct_cfg.get("loss_pct", 2.5) / 100
                if pos.side == "LONG":
                    stop_price = pos.average_entry_price * (1 - loss_pct)
                else:
                    stop_price = pos.average_entry_price * (1 + loss_pct)
                max_dca = cfg["trading"].get("max_dca_steps", 4)
                grid_levels.append({
                    "price": stop_price,
                    "label": f"STOP {pct_cfg.get('loss_pct', 2.5)}%"
                             + (" (aktif)" if pos.dca_fills_count >= max_dca else ""),
                    "filled": pos.dca_fills_count >= max_dca,
                })

    # Sort markers by time
    markers.sort(key=lambda m: m["time"])

    return {
        "symbol": symbol,
        "interval": interval,
        "candles": candles,
        "markers": markers,
        "grid_levels": grid_levels if sim and pos and pos.condition != 0.0 else [],
    }


# ══════════════════════════════════════════════════════════════════════
# BACKTEST ENDPOINTS — completely independent from dry-run
# ══════════════════════════════════════════════════════════════════════

_bt_state: dict[str, Any] = {
    "running": False,
    "instance": None,
    "result": None,
    "error": None,
}


def _run_backtest(symbols: list[str], lookback_days: int, config: dict) -> None:
    """Background thread target for backtesting."""
    try:
        bt = Backtester(config)
        _bt_state["instance"] = bt
        result = bt.run(symbols, lookback_days)
        _bt_state["result"] = {
            "trades": result.trades,
            "equity_curve": result.equity_curve,
            "drawdown_curve": result.drawdown_curve,
            "metrics": result.metrics,
            "per_symbol": result.per_symbol,
        }
    except Exception as e:
        logger.error("Backtest failed: %s", str(e)[:200])
        _bt_state["error"] = str(e)
    finally:
        _bt_state["running"] = False


@app.post("/api/backtest/run")
def start_backtest(body: dict):
    """Start a backtest in a background thread."""
    if _bt_state["running"]:
        return {"error": "Backtest already running"}

    symbols = body.get("symbols", [])
    if not symbols:
        return {"error": "No symbols provided"}

    lookback_days = body.get("lookback_days", 30)
    config = body.get("config", state["config"])

    _bt_state["running"] = True
    _bt_state["result"] = None
    _bt_state["error"] = None
    _bt_state["instance"] = None

    thread = threading.Thread(
        target=_run_backtest,
        args=(symbols, lookback_days, config),
        daemon=True,
    )
    thread.start()

    return {"status": "started", "symbols": len(symbols), "lookback_days": lookback_days}


@app.get("/api/backtest/status")
def backtest_status():
    """Poll backtest progress."""
    bt = _bt_state.get("instance")
    return {
        "running": _bt_state["running"],
        "progress": round(bt.progress, 1) if bt else (100.0 if _bt_state["result"] else 0),
        "status": bt.status if bt else ("done" if _bt_state["result"] else "idle"),
        "error": _bt_state["error"],
    }


@app.get("/api/backtest/results")
def backtest_results():
    """Return full backtest results."""
    result = _bt_state["result"]
    if not result:
        return {"status": "no_results"}
    return result


@app.post("/api/backtest/reset")
def backtest_reset():
    """Reset backtest state so a new one can be started."""
    if _bt_state["running"]:
        return {"error": "Backtest is still running"}
    _bt_state["running"] = False
    _bt_state["instance"] = None
    _bt_state["result"] = None
    _bt_state["error"] = None
    return {"status": "reset"}


# ══════════════════════════════════════════════════════════════════════
# FAST BACKTEST — numpy array engine (seconds, not hours)
# ══════════════════════════════════════════════════════════════════════

_fast_bt_state: dict[str, Any] = {
    "running": False,
    "progress": 0,
    "result": None,
    "error": None,
}


def _run_fast_backtest(symbol: str, days: int, config: dict, oos_only: bool = True) -> None:
    try:
        from core.engine.fast_backtest import fetch_and_cache_klines, run_fast_backtest
        import copy

        # Deep copy config to avoid mutation
        config = copy.deepcopy(config)

        _fast_bt_state["progress"] = 10
        project_root = str(Path(__file__).resolve().parent.parent)
        cache_dir = str(Path(project_root) / "data")
        df = fetch_and_cache_klines(symbol, "3m", days, cache_dir=cache_dir)
        logger.info("[FAST_BT] Loaded %d bars, oos_only=%s", len(df), oos_only)
        _fast_bt_state["progress"] = 30

        # OOS only: use last 30% of data (same as optimizer)
        if oos_only:
            si = int(len(df) * 0.7)
            df = df.iloc[si:].reset_index(drop=True)
            logger.info("[FAST_BT] OOS split: %d bars", len(df))

        # Log config + module source for debugging
        from core.strategy.indicators import adaptive_pmax as _ap
        import inspect
        src_lines = inspect.getsource(_ap).split('\n')
        # Check if it has ma_cache (new version) or flip_count_history (old version)
        has_ma_cache = any('ma_cache' in l for l in src_lines)
        has_flip_history = any('flip_count_history' in l for l in src_lines)
        logger.info("[FAST_BT] adaptive_pmax version: ma_cache=%s flip_history=%s lines=%d",
                    has_ma_cache, has_flip_history, len(src_lines))

        t = config.get("trading", {})
        p = config.get("strategy", {}).get("timeframes", [{}])[0].get("pmax", {})
        logger.info("[FAST_BT] Config: bal=%.0f lev=%d hard_stop=%s dyn_sl=%s pmax_atr_period=%s ma_length=%s",
                    t.get("initial_balance"), t.get("leverage"),
                    t.get("hard_stop", {}).get("enabled"),
                    t.get("dynamic_sl", {}).get("enabled"),
                    p.get("atr_period"), p.get("ma_length"))

        result = run_fast_backtest(df, config, symbol=symbol)
        logger.info("[FAST_BT] Result: Net=%.1f%% Bal=$%.0f DD=%.1f%%",
                    result.metrics["total_pnl_pct"], result.metrics["current_balance"],
                    result.metrics["max_drawdown_pct"])
        _fast_bt_state["progress"] = 100
        _fast_bt_state["result"] = {
            "trades": result.trades,
            "equity_curve": result.equity_curve,
            "drawdown_curve": result.drawdown_curve,
            "metrics": result.metrics,
            "per_symbol": result.per_symbol,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error("Fast backtest failed: %s", str(e)[:200])
        _fast_bt_state["error"] = str(e)
    finally:
        _fast_bt_state["running"] = False


@app.post("/api/backtest/fast")
def start_fast_backtest(body: dict):
    """Start fast numpy backtest (seconds, not hours)."""
    if _fast_bt_state["running"]:
        return {"error": "Fast backtest already running"}

    symbol = body.get("symbol", "ETHUSDT")
    days = body.get("days", 180)
    # OOS split disabled by default — user can enable via oos_only param
    oos_only = body.get("oos_only", False)

    _fast_bt_state["running"] = True
    _fast_bt_state["progress"] = 0
    _fast_bt_state["result"] = None
    _fast_bt_state["error"] = None

    thread = threading.Thread(
        target=_run_fast_backtest,
        args=(symbol, days, state["config"], oos_only),
        daemon=True,
    )
    thread.start()

    return {"status": "started", "symbol": symbol, "days": days}


@app.get("/api/backtest/fast/status")
def fast_backtest_status():
    return {
        "running": _fast_bt_state["running"],
        "progress": _fast_bt_state["progress"],
        "error": _fast_bt_state["error"],
    }


@app.get("/api/backtest/fast/results")
def fast_backtest_results():
    result = _fast_bt_state["result"]
    if not result:
        return {"status": "no_results"}
    return result


# ══════════════════════════════════════════════════════════════════════
# WALK-FORWARD OPTIMIZATION
# ══════════════════════════════════════════════════════════════════════

_wf_state: dict[str, Any] = {
    "running": False,
    "result": None,
    "error": None,
}


def _run_walk_forward(symbol: str, days: int, trials: int) -> None:
    try:
        from core.engine.fast_backtest import fetch_and_cache_klines
        from core.engine.walk_forward import run_walk_forward

        project_root = str(Path(__file__).resolve().parent.parent)
        cache_dir = str(Path(project_root) / "data")
        df = fetch_and_cache_klines(symbol, "3m", days, cache_dir=cache_dir)

        result = run_walk_forward(
            df, is_days=90, oos_days=30, step_days=30,
            trials_per_window=trials, leverage=40,
            initial_balance=10000.0, symbol=symbol,
        )
        _wf_state["result"] = result
    except Exception as e:
        import traceback
        traceback.print_exc()
        _wf_state["error"] = str(e)
    finally:
        _wf_state["running"] = False


@app.post("/api/backtest/walkforward")
def start_walk_forward(body: dict):
    if _wf_state["running"]:
        return {"error": "Walk-forward already running"}
    symbol = body.get("symbol", "ETHUSDT")
    days = body.get("days", 250)
    trials = body.get("trials_per_window", 100)

    _wf_state["running"] = True
    _wf_state["result"] = None
    _wf_state["error"] = None

    thread = threading.Thread(
        target=_run_walk_forward, args=(symbol, days, trials), daemon=True,
    )
    thread.start()
    return {"status": "started", "symbol": symbol, "days": days, "trials_per_window": trials}


@app.get("/api/backtest/walkforward/status")
def walk_forward_status():
    from core.engine.walk_forward import wf_progress
    return {
        "running": _wf_state["running"],
        "window": wf_progress.get("window", 0),
        "total_windows": wf_progress.get("total_windows", 0),
        "phase": wf_progress.get("phase", "idle"),
        "error": _wf_state["error"],
    }


@app.get("/api/backtest/walkforward/results")
def walk_forward_results():
    result = _wf_state["result"]
    if not result:
        return {"status": "no_results"}
    return result


# ══════════════════════════════════════════════════════════════════════
# LIVE TRADING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

_live_state: dict[str, Any] = {
    "running": False,
    "executor": None,
    "client": None,
    "active_symbols": [],
    "pair_configs": {},      # symbol → {margin, leverage}
    "scan_results": {},
    "signal_log": [],
    "api_key": "",
    "api_secret": "",
    "testnet": False,
}
_live_lock = threading.Lock()
_live_ws_instance: BinanceWS | None = None
_live_ws_book_data: dict[str, dict[str, float]] = {}
_live_ws_book_lock = threading.Lock()


@app.get("/api/live/balance")
def live_balance():
    """Fetch Binance Futures USDT balance. Requires API keys."""
    api_key = _live_state.get("api_key", "")
    api_secret = _live_state.get("api_secret", "")
    testnet = _live_state.get("testnet", False)

    if not api_key or not api_secret:
        return {"error": "API keys not configured"}

    try:
        client = BinanceFutures(api_key, api_secret, testnet=testnet)
        bal = client.get_balance()
        return {"balance": bal["balance"], "available": bal["available"], "unrealized_pnl": bal["unrealized_pnl"]}
    except Exception as e:
        return {"error": str(e)[:200]}


@app.post("/api/live/keys")
def live_set_keys(body: dict):
    """Save API keys (session only — not persisted to disk)."""
    _live_state["api_key"] = body.get("api_key", "")
    _live_state["api_secret"] = body.get("api_secret", "")
    _live_state["testnet"] = body.get("testnet", False)

    # Validate keys by fetching balance
    try:
        client = BinanceFutures(
            _live_state["api_key"], _live_state["api_secret"],
            testnet=_live_state["testnet"],
        )
        bal = client.get_balance()
        return {
            "status": "ok",
            "balance": bal["balance"],
            "available": bal["available"],
        }
    except Exception as e:
        _live_state["api_key"] = ""
        _live_state["api_secret"] = ""
        return {"error": f"Invalid API keys: {str(e)[:200]}"}


@app.get("/api/live/positions")
def live_exchange_positions():
    """Get existing open positions from Binance exchange."""
    api_key = _live_state.get("api_key", "")
    api_secret = _live_state.get("api_secret", "")
    if not api_key:
        return {"error": "API keys not configured"}

    try:
        client = BinanceFutures(api_key, api_secret, testnet=_live_state.get("testnet", False))
        positions = client.get_positions()
        return {"positions": positions}
    except Exception as e:
        return {"error": str(e)[:200]}


def _live_signal_scanner_loop() -> None:
    """Background thread: signal scanner for live trading mode.

    Same logic as dry-run scanner, but uses LiveExecutor instead of Simulator.
    """
    logger.info("Live signal scanner started")
    last_scan_bucket = 0

    while _live_state["running"]:
        time.sleep(5)

        if not _live_state["running"] or not _live_state["active_symbols"]:
            continue

        cfg = state["config"]
        tf = cfg["strategy"]["timeframe"]
        interval_s = _TIMEFRAME_SECONDS.get(tf, 900)

        now_ts = int(time.time())
        current_bucket = now_ts // interval_s
        if current_bucket == last_scan_bucket:
            continue
        last_scan_bucket = current_bucket
        time.sleep(10)

        logger.info("Live scanner: new %s candle — rescanning %d symbols",
                     tf, len(_live_state["active_symbols"]))

        executor: LiveExecutor | None = _live_state["executor"]
        if not executor:
            continue

        # ── Position Sync — reconcile with exchange every ~60s ──
        with _live_lock:
            sync_warnings = executor.sync_positions()
            if sync_warnings:
                for w in sync_warnings:
                    _live_state["signal_log"].append({
                        "time": time.strftime("%H:%M:%S"),
                        "symbol": "SYSTEM",
                        "side": "SYNC",
                        "price": 0,
                        "rsi": 0,
                        "source": w,
                    })

            # ── Circuit breaker check ──
            if executor.circuit_breaker_triggered:
                logger.critical(
                    "[LIVE] Circuit breaker triggered: %s — stopping new entries",
                    executor.circuit_breaker_reason,
                )
                _live_state["signal_log"].append({
                    "time": time.strftime("%H:%M:%S"),
                    "symbol": "SYSTEM",
                    "side": "STOP",
                    "price": 0,
                    "rsi": 0,
                    "source": f"CIRCUIT_BREAKER: {executor.circuit_breaker_reason}",
                })
                # Don't stop scanning (SL still needs monitoring), but no new entries
                # The circuit breaker inside process_signal will block new entries

        rest: BinanceRest = state["rest"]

        for sym in list(_live_state["active_symbols"]):
            if not _live_state["running"]:
                break
            try:
                klines = rest.fetch_klines_sync(sym, tf, limit=1500)
                if len(klines) < 200:
                    continue

                last_closed = klines[-2] if len(klines) >= 2 else klines[-1]
                klines_for_signal = klines[:-1]

                df = pd.DataFrame(klines_for_signal)
                df["symbol"] = sym

                engine = SignalEngine(cfg)
                signal = engine.process(df)

                with _live_lock:
                    # TP/SL check on last closed candle
                    if executor.has_position(sym):
                        candle_high = float(last_closed["high"])
                        candle_low = float(last_closed["low"])
                        candle_close = float(last_closed["close"])
                        close_time = int(last_closed.get("close_time", 0))
                        # Calculate current ATR for DynSL
                        from core.strategy.indicators import atr as atr_indicator
                        dyn_sl_period = cfg["trading"].get("dynamic_sl", {}).get("atr_period", 12)
                        _current_atr = 0.0
                        if len(df) > dyn_sl_period:
                            _atr_s = atr_indicator(df["high"], df["low"], df["close"], dyn_sl_period)
                            _current_atr = float(_atr_s.iloc[-1]) if not pd.isna(_atr_s.iloc[-1]) else 0.0
                        exit_trades = executor.process_candle(sym, candle_high, candle_low, close_time, candle_close=candle_close, current_atr=_current_atr)
                        for t in exit_trades:
                            _live_state["signal_log"].append({
                                "time": time.strftime("%H:%M:%S"),
                                "symbol": sym,
                                "side": t.side,
                                "price": t.exit_price,
                                "rsi": 0,
                                "source": f"LIVE_EXIT_{t.exit_reason}",
                            })
                        # Update scan_results if position fully closed by TP/SL
                        if not executor.has_position(sym):
                            _live_state["scan_results"][sym] = {
                                "status": "closed_tp",
                                "side": _live_state["scan_results"].get(sym, {}).get("side", ""),
                                "price": _live_state["scan_results"].get(sym, {}).get("price", 0),
                                "last_price": float(last_closed["close"]),
                            }

                    if signal:
                        has_pos = executor.has_position(sym)
                        if has_pos:
                            existing = executor.positions[sym]
                            if existing.side == signal.side:
                                continue

                        reversal_trades = executor.process_signal(signal)
                        for rt in reversal_trades:
                            _live_state["signal_log"].append({
                                "time": time.strftime("%H:%M:%S"),
                                "symbol": sym,
                                "side": rt.side,
                                "price": rt.exit_price,
                                "rsi": 0,
                                "source": f"LIVE_EXIT_{rt.exit_reason}",
                            })

                        pos = executor.positions.get(sym)
                        if pos and pos.condition != 0.0:
                            logger.info(
                                "[LIVE_ENTRY] %s %s @ %.4f",
                                sym, signal.side, signal.price,
                            )

                        _live_state["signal_log"].append({
                            "time": time.strftime("%H:%M:%S"),
                            "symbol": sym,
                            "side": signal.side,
                            "price": signal.price,
                            "rsi": round(signal.rsi_value, 2),
                            "source": "LIVE_SIGNAL",
                        })

                        _live_state["scan_results"][sym] = {
                            "status": "signal",
                            "side": signal.side,
                            "price": signal.price,
                            "rsi": round(signal.rsi_value, 2),
                            "atr": round(signal.atr_value, 4),
                            "last_price": float(df["close"].iloc[-1]),
                        }

            except Exception as e:
                logger.error("Live scanner error for %s: %s", sym, str(e)[:100])


async def _on_live_book_ticker(ticker: dict) -> None:
    """Update live mode book ticker cache."""
    with _live_ws_book_lock:
        _live_ws_book_data[ticker["symbol"]] = ticker


def _start_live_ws_loop(symbols: list[str]) -> None:
    """Start WS bookTicker for live mode."""
    global _live_ws_instance
    loop = asyncio.new_event_loop()

    async def _run():
        global _live_ws_instance
        ws = BinanceWS(on_candle=_on_candle_noop, on_book_ticker=_on_live_book_ticker)
        _live_ws_instance = ws
        await ws.connect()
        for sym in symbols:
            await ws.subscribe_book_ticker(sym)
        logger.info("Live WS bookTicker subscribed for %d symbols", len(symbols))
        while _live_state["running"]:
            await asyncio.sleep(1)
        try:
            await ws.close()
        except Exception:
            pass
        _live_ws_instance = None

    loop.run_until_complete(_run())
    loop.close()


@app.post("/api/live/start")
def live_start(body: dict):
    """Start live trading mode.

    Body: {
        pair_configs: {BTCUSDT: {margin: 50, leverage: 20}, ...}
    }
    """
    if _live_state["running"]:
        return {"error": "Live mode already running"}

    api_key = _live_state.get("api_key", "")
    api_secret = _live_state.get("api_secret", "")
    if not api_key or not api_secret:
        return {"error": "API keys not configured"}

    pair_configs = body.get("pair_configs", {})
    if not pair_configs:
        return {"error": "No pair configs provided"}

    cfg = state["config"]

    # Apply frontend strategy overrides if provided
    strategy_overrides = body.get("strategy", {})
    if strategy_overrides:
        cfg["strategy"].update(strategy_overrides)

    # Create Binance client and LiveExecutor
    client = BinanceFutures(api_key, api_secret, testnet=_live_state.get("testnet", False))
    executor = LiveExecutor(client, cfg)

    # Apply frontend protection overrides if provided
    protection = body.get("protection", {})
    if "max_drawdown_pct" in protection:
        executor._max_drawdown_pct = float(protection["max_drawdown_pct"])
    if "max_total_margin_pct" in protection:
        executor._max_total_margin_pct = float(protection["max_total_margin_pct"])
    if "max_open_positions" in protection:
        executor._max_open_positions = int(protection["max_open_positions"])

    # Configure each pair
    symbols = []
    for sym, pc in pair_configs.items():
        margin = float(pc.get("margin", 100))
        leverage = int(pc.get("leverage", 10))
        executor.configure_pair(sym, margin, leverage)
        symbols.append(sym)

    # Fetch initial balance
    bal = executor.refresh_balance()

    # Load existing exchange positions for display
    exchange_positions = executor.load_exchange_positions()

    # Run initial scan (same as dry-run) to detect current signal state
    scan_results = {}
    signal_log = []
    rest: BinanceRest = state["rest"]

    for sym in symbols:
        try:
            klines = rest.fetch_klines_sync(sym, cfg["strategy"]["timeframe"], limit=1500)
            if len(klines) > 1:
                klines = klines[:-1]
            if len(klines) < 200:
                scan_results[sym] = {"status": "insufficient_data", "candles": len(klines)}
                continue

            df = pd.DataFrame(klines)
            df["symbol"] = sym

            engine = SignalEngine(cfg)
            signal = engine.process_backfill(df)

            # Also check forming bar for a more recent crossover
            live_signal = engine.process(df)
            if live_signal:
                if signal is None or live_signal.side != signal.side:
                    logger.info(
                        "[LIVE_INIT] %s forming-bar override: backfill=%s → live=%s",
                        sym,
                        signal.side if signal else "None",
                        live_signal.side,
                    )
                    signal = live_signal

            last_price = float(df["close"].iloc[-1])

            if signal:
                scan_results[sym] = {
                    "status": "signal_detected",
                    "side": signal.side,
                    "price": signal.price,
                    "rsi": round(signal.rsi_value, 2),
                    "atr": round(signal.atr_value, 4),
                    "last_price": last_price,
                    "pair_state": "OBSERVING",
                }
                signal_log.append({
                    "time": time.strftime("%H:%M:%S"),
                    "symbol": sym,
                    "side": signal.side,
                    "price": signal.price,
                    "rsi": round(signal.rsi_value, 2),
                    "source": "INITIAL_SCAN",
                })
            else:
                from core.strategy.indicators import pmax as calc_pmax, rsi as calc_rsi
                pmax_cfg = cfg["strategy"].get("pmax", {})
                src_type = pmax_cfg.get("source", "hl2").lower()
                if src_type == "hl2":
                    src = (df["high"] + df["low"]) / 2
                elif src_type == "hlc3":
                    src = (df["high"] + df["low"] + df["close"]) / 3
                elif src_type == "ohlc4":
                    src = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
                else:
                    src = df["close"]
                _, mavg, direction = calc_pmax(
                    src, df["high"], df["low"], df["close"],
                    atr_period=pmax_cfg.get("atr_period", 10),
                    atr_multiplier=pmax_cfg.get("atr_multiplier", 3.0),
                    ma_type=pmax_cfg.get("ma_type", "EMA"),
                    ma_length=pmax_cfg.get("ma_length", 10),
                    change_atr=pmax_cfg.get("change_atr", True),
                    normalize_atr=pmax_cfg.get("normalize_atr", False),
                )
                trend = "BULLISH" if direction.iloc[-1] == 1 else "BEARISH"
                rsi_val = calc_rsi(df["close"], 28).iloc[-1]
                scan_results[sym] = {
                    "status": "monitoring",
                    "trend": trend,
                    "last_price": last_price,
                    "rsi": round(float(rsi_val), 2),
                    "pair_state": "OBSERVING",
                }
        except Exception as e:
            scan_results[sym] = {"status": "error", "message": str(e)[:100]}

    # Save state
    _live_state["running"] = True
    _live_state["executor"] = executor
    _live_state["client"] = client
    _live_state["active_symbols"] = symbols
    _live_state["pair_configs"] = pair_configs
    _live_state["scan_results"] = scan_results
    _live_state["signal_log"] = signal_log

    # Start WS bookTicker
    _live_ws_book_data.clear()
    ws_thread = threading.Thread(target=_start_live_ws_loop, args=(symbols,), daemon=True)
    ws_thread.start()

    # Start live signal scanner
    scanner_thread = threading.Thread(target=_live_signal_scanner_loop, daemon=True)
    scanner_thread.start()

    return {
        "status": "started",
        "pairs": len(symbols),
        "balance": bal,
        "exchange_positions": exchange_positions,
        "scan_results": scan_results,
    }


@app.post("/api/live/stop")
def live_stop():
    """Stop live trading. Does NOT close open positions."""
    _live_state["running"] = False
    _live_state["active_symbols"] = []
    _live_state["scan_results"] = {}
    _live_state["signal_log"] = []

    # Keep executor reference for stats but mark as stopped
    _live_ws_book_data.clear()
    global _live_ws_instance
    _live_ws_instance = None

    return {"status": "stopped"}


@app.post("/api/live/protection")
def live_update_protection(body: dict):
    """Update account protection settings at runtime.

    Body: {max_drawdown_pct: 40, max_total_margin_pct: 70, max_open_positions: 5}
    """
    executor: LiveExecutor | None = _live_state.get("executor")
    if not executor:
        return {"error": "No live executor"}

    with _live_lock:
        if "max_drawdown_pct" in body:
            executor._max_drawdown_pct = float(body["max_drawdown_pct"])
        if "max_total_margin_pct" in body:
            executor._max_total_margin_pct = float(body["max_total_margin_pct"])
        if "max_open_positions" in body:
            executor._max_open_positions = int(body["max_open_positions"])

    return {
        "status": "ok",
        "max_drawdown_pct": executor._max_drawdown_pct,
        "max_total_margin_pct": executor._max_total_margin_pct,
        "max_open_positions": executor._max_open_positions,
    }


@app.get("/api/live/protection")
def live_get_protection():
    """Get current protection settings."""
    executor: LiveExecutor | None = _live_state.get("executor")
    if executor:
        return {
            "max_drawdown_pct": executor._max_drawdown_pct,
            "max_total_margin_pct": executor._max_total_margin_pct,
            "max_open_positions": executor._max_open_positions,
            "circuit_breaker": executor.circuit_breaker_triggered,
            "circuit_breaker_reason": executor.circuit_breaker_reason,
        }
    # Fallback to config defaults
    cfg = state["config"]
    prot = cfg.get("protection", {})
    return {
        "max_drawdown_pct": prot.get("max_drawdown_pct", 40.0),
        "max_total_margin_pct": prot.get("max_total_margin_pct", 70.0),
        "max_open_positions": prot.get("max_open_positions", 5),
        "circuit_breaker": False,
        "circuit_breaker_reason": "",
    }


@app.post("/api/live/reset-circuit-breaker")
def live_reset_circuit_breaker():
    """Manually reset the circuit breaker after user review."""
    executor: LiveExecutor | None = _live_state.get("executor")
    if not executor:
        return {"error": "No live executor"}

    with _live_lock:
        executor.reset_circuit_breaker()

    return {"status": "reset", "circuit_breaker": False}


@app.post("/api/live/emergency-close")
def live_emergency_close():
    """Emergency: close all positions immediately."""
    executor: LiveExecutor | None = _live_state.get("executor")
    if not executor:
        return {"error": "No live executor"}

    with _live_lock:
        trades = executor.emergency_close_all()

    return {
        "status": "closed",
        "trades_closed": len(trades),
        "trades": [
            {"symbol": t.symbol, "side": t.side, "pnl_usdt": t.pnl_usdt}
            for t in trades
        ],
    }


@app.get("/api/live/status")
def live_status():
    """Main polling endpoint for live mode — returns full dashboard state."""
    executor: LiveExecutor | None = _live_state.get("executor")
    cfg = state["config"]

    if not executor:
        return {
            "live_running": False,
            "balance": 0,
            "available": 0,
            "positions": [],
            "pair_summaries": {},
            "stats": {},
            "signal_log": [],
            "trade_log": [],
            "totals": {},
        }

    # Refresh balance periodically (not every poll — expensive)
    # The executor caches balance from last refresh

    # Fetch live orderbook
    orderbook = {}
    live_prices = {}
    symbols = _live_state.get("active_symbols", [])

    if _live_state["running"] and symbols:
        with _live_ws_book_lock:
            ws_data = {sym: _live_ws_book_data[sym] for sym in symbols if sym in _live_ws_book_data}
        orderbook = ws_data
        for sym, ob in orderbook.items():
            live_prices[sym] = (ob["bid"] + ob["ask"]) / 2

    # Positions with live PnL (thread-safe snapshot)
    positions = []
    with _live_lock:
        position_snapshot = [
            (sym, pos) for sym, pos in executor.positions.items()
            if pos.condition != 0.0
        ]
        scan_results_snapshot = dict(_live_state.get("scan_results", {}))
        signal_log_snapshot = list(_live_state.get("signal_log", []))[-50:]
        stats = executor.get_stats()

    for sym, pos in position_snapshot:
        pc = _live_state["pair_configs"].get(sym, {})
        margin = float(pc.get("margin", 100))
        leverage = int(pc.get("leverage", 10))

        ob = orderbook.get(sym)
        if ob:
            mark_price = ob["ask"] if pos.side == "LONG" else ob["bid"]
            bid = ob["bid"]
            ask = ob["ask"]
            spread = ask - bid
        else:
            mark_price = pos.entry_price
            bid = mark_price
            ask = mark_price
            spread = 0.0

        notional = margin * leverage * pos.remaining_qty

        if pos.side == "LONG":
            upnl_pct = (mark_price - pos.entry_price) / pos.entry_price * 100
        else:
            upnl_pct = (pos.entry_price - mark_price) / pos.entry_price * 100
        upnl_usdt = notional * upnl_pct / 100

        positions.append({
            "symbol": sym,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "mark_price": round(mark_price, 4),
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "spread": round(spread, 6),
            "notional_usdt": round(notional, 2),
            "condition": pos.condition,
            "remaining_qty": pos.remaining_qty,
            "unrealized_pnl_usdt": round(upnl_usdt, 4),
            "unrealized_pnl_pct": round(upnl_pct, 4),
            "pair_state": executor.get_pair_state(sym),
            "margin": margin,
            "leverage": leverage,
        })

    # Per-pair summaries
    pair_summaries = {}
    for sym in symbols:
        sym_trades = [t for t in executor.trades if t.symbol == sym]
        sym_realized = sum(t.pnl_usdt for t in sym_trades)
        sym_fees = sum(t.fee_usdt for t in sym_trades)

        pos_match = next((p for p in positions if p["symbol"] == sym), None)
        sym_unrealized = pos_match["unrealized_pnl_usdt"] if pos_match else 0.0
        current_price = live_prices.get(sym, 0)

        ob = orderbook.get(sym, {})
        scan = scan_results_snapshot.get(sym, {})
        pair_summaries[sym] = {
            "last_price": round(current_price, 4),
            "bid": round(ob.get("bid", current_price), 4),
            "ask": round(ob.get("ask", current_price), 4),
            "spread": round(ob.get("ask", 0) - ob.get("bid", 0), 6) if ob else 0,
            "status": scan.get("status", "waiting"),
            "trend": scan.get("trend", ""),
            "side": scan.get("side", ""),
            "rsi": scan.get("rsi", 0),
            "unrealized_pnl": round(sym_unrealized, 4),
            "realized_pnl": round(sym_realized, 4),
            "total_pnl": round(sym_unrealized + sym_realized, 4),
            "fees": round(sym_fees, 4),
            "trade_count": len(sym_trades),
            "pair_state": executor.get_pair_state(sym),
        }

    total_unrealized = sum(p["unrealized_pnl_usdt"] for p in positions)

    return {
        "live_running": _live_state["running"],
        "balance": round(executor.balance, 2),
        "available": round(executor.available_balance, 2),
        "active_symbols": symbols,
        "stats": stats,
        "positions": positions,
        "pair_summaries": pair_summaries,
        "signal_log": signal_log_snapshot,
        "trade_log": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "pnl_usdt": t.pnl_usdt,
                "pnl_pct": t.pnl_percent,
                "fee_usdt": t.fee_usdt,
                "leverage": t.leverage,
            }
            for t in executor.trades
        ],
        "totals": {
            "unrealized_pnl": round(total_unrealized, 4),
            "realized_pnl": round(stats["total_pnl"], 4),
            "total_pnl": round(total_unrealized + stats["total_pnl"], 4),
            "total_fees": round(stats["total_fees"], 4),
            "net_pnl": round(total_unrealized + stats["total_pnl"] - stats["total_fees"], 4),
        },
        "pair_configs": _live_state.get("pair_configs", {}),
    }
