"""FastAPI backend — REST API + WS status for the Scalper Bot dashboard."""

from __future__ import annotations

import time
import secrets
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from core.data.binance_rest import BinanceRest
from core.data.binance_ws import BinanceWS, BinanceUserDataWS, DualUserDataWS
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

# ── Monitor Tokens — {token: {created_at, label}} ──
_monitor_tokens: dict[str, dict[str, Any]] = {}

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
    """Update config from frontend.

    Bot çalışırken: sadece trading params güncellenir, simulator resetlenir.
    Bot kapalıyken: tüm config güncellenir.
    """
    cfg = state["config"]
    if "trading" in body:
        cfg["trading"].update(body["trading"])
    if "strategy" in body:
        cfg["strategy"].update(body["strategy"])
    if "risk" in body:
        cfg["risk"].update(body["risk"])

    # Bot çalışmıyorsa veya simulator yoksa → yeni simulator oluştur
    if not state["bot_running"] or not state.get("simulator"):
        state["simulator"] = Simulator(cfg)
    else:
        # Bot çalışırken → mevcut trade history'yi koru, engine'i resetle
        with _sim_lock:
            old_sim = state["simulator"]
            old_trades = list(old_sim.trades)  # trade history'yi koru
            new_sim = Simulator(cfg)
            new_sim.trades = old_trades
            state["simulator"] = new_sim
            logger.info("Config updated while bot running — simulator reset (trades preserved: %d)", len(old_trades))

    return {"status": "ok", "bot_running": state["bot_running"]}


@app.post("/api/bot/start")
def start_bot(body: dict):
    """Start bot: run initial scan on selected pairs."""
    symbols = body.get("symbols", [])
    if not symbols:
        return {"error": "No symbols provided"}

    state["active_symbols"] = symbols
    state["ws_connected"] = True
    state["ws_last_ping"] = time.time()

    # Reset simulator — bot_running set AFTER backfill to prevent scanner race
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

    # Backfill complete — NOW enable bot_running so scanner can start
    state["bot_running"] = True

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
def get_chart_data(symbol: str = "BTCUSDT", limit: int = 500, source: str = "dryrun"):
    """Return OHLC candles + PMax indicator + trade markers for charting.

    source: "dryrun" (default) or "live" — determines which executor's trades to show.
    """
    cfg = state["config"]
    # Live mode: testnet ise testnet REST kullan
    if source == "live" and _live_state.get("rest"):
        rest: BinanceRest = _live_state["rest"]
    else:
        rest: BinanceRest = state["rest"]

    tf_configs = cfg["strategy"].get("timeframes", [])
    if not tf_configs:
        return {"error": "No timeframes configured"}
    tf_cfg = tf_configs[0]
    strategy_interval = tf_cfg.get("timeframe", "3m")
    chart_interval = "1m"  # Chart always shows 1m candles

    # Fetch 1m klines for chart display
    try:
        klines_1m = rest.fetch_klines_sync(symbol, chart_interval, limit=limit)
    except Exception as e:
        return {"error": str(e)}

    if len(klines_1m) < 50:
        return {"error": "Insufficient data"}

    df_1m = pd.DataFrame(klines_1m)
    df_1m["symbol"] = symbol

    # Fetch strategy-timeframe klines for indicator calculation
    # 1m limit / 3 = approximate 3m bars needed
    strategy_limit = max(500, limit // 3 + 100)
    try:
        klines_strategy = rest.fetch_klines_sync(symbol, strategy_interval, limit=strategy_limit)
    except Exception as e:
        return {"error": str(e)}

    df_strategy = pd.DataFrame(klines_strategy)

    # Compute PMax from strategy timeframe (3m)
    from core.strategy.indicators import pmax as calc_pmax, adaptive_pmax as calc_adaptive_pmax
    pmax_cfg = tf_cfg.get("pmax", {})
    src_type = pmax_cfg.get("source", "hl2").lower()
    if src_type == "hl2":
        src = (df_strategy["high"] + df_strategy["low"]) / 2
    elif src_type == "hlc3":
        src = (df_strategy["high"] + df_strategy["low"] + df_strategy["close"]) / 3
    elif src_type == "ohlc4":
        src = (df_strategy["open"] + df_strategy["high"] + df_strategy["low"] + df_strategy["close"]) / 4
    else:
        src = df_strategy["close"]

    if pmax_cfg.get("adaptive", False):
        pmax_line, mavg, direction = calc_adaptive_pmax(
            src, df_strategy["high"], df_strategy["low"], df_strategy["close"], pmax_cfg,
        )
    else:
        pmax_line, mavg, direction = calc_pmax(
            src, df_strategy["high"], df_strategy["low"], df_strategy["close"],
            atr_period=pmax_cfg.get("atr_period", 10),
            atr_multiplier=pmax_cfg.get("atr_multiplier", 3.0),
            ma_type=pmax_cfg.get("ma_type", "EMA"),
            ma_length=pmax_cfg.get("ma_length", 10),
            change_atr=pmax_cfg.get("change_atr", True),
            normalize_atr=pmax_cfg.get("normalize_atr", False),
        )

    # Compute Keltner Channel from strategy timeframe (3m)
    from core.strategy.indicators import keltner_channel as calc_kc
    kc_cfg = tf_cfg.get("keltner", {})
    kc_mid, kc_upper_line, kc_lower_line = calc_kc(
        df_strategy["high"], df_strategy["low"], df_strategy["close"],
        kc_length=kc_cfg.get("length", 20),
        kc_multiplier=kc_cfg.get("multiplier", 1.5),
        atr_period=kc_cfg.get("atr_period", 10),
    )

    # Build lookup: 3m open_time → indicator values
    indicator_map = {}
    for i in range(len(df_strategy)):
        t_3m = int(df_strategy["open_time"].iloc[i])
        pmax_val = float(pmax_line.iloc[i]) if not pd.isna(pmax_line.iloc[i]) else None
        kc_u_val = float(kc_upper_line.iloc[i]) if not pd.isna(kc_upper_line.iloc[i]) else None
        kc_l_val = float(kc_lower_line.iloc[i]) if not pd.isna(kc_lower_line.iloc[i]) else None
        mavg_val = float(kc_mid.iloc[i]) if not pd.isna(kc_mid.iloc[i]) else None
        dir_val = int(direction.iloc[i]) if not pd.isna(direction.iloc[i]) else 0
        indicator_map[t_3m] = {
            "pmax": pmax_val, "mavg": mavg_val,
            "kc_upper": kc_u_val, "kc_lower": kc_l_val, "direction": dir_val,
        }

    # Map 1m candles to 3m indicators
    # Each 1m candle belongs to a 3m bar: floor(1m_open_time / (3*60*1000)) * (3*60*1000)
    strategy_ms = 3 * 60 * 1000  # 3 minutes in ms
    candles = []
    for i, row in df_1m.iterrows():
        t_1m = int(row["open_time"])
        t_3m_bucket = (t_1m // strategy_ms) * strategy_ms
        indicators = indicator_map.get(t_3m_bucket, {})

        candles.append({
            "time": t_1m // 1000,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "pmax": indicators.get("pmax"),
            "mavg": indicators.get("mavg"),
            "kc_upper": indicators.get("kc_upper"),
            "kc_lower": indicators.get("kc_lower"),
            "direction": indicators.get("direction", 0),
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

    # ── Live trade markers (when source=live) ──
    live_markers = []
    if source == "live":
        executor: LiveExecutor | None = _live_state.get("executor")
        if executor:
            for trade in executor.trades:
                if trade.symbol != symbol:
                    continue
                t = trade.entry_time // 1000 if trade.entry_time > 0 else 0
                exit_t = trade.exit_time // 1000 if trade.exit_time > 0 else 0

                reason = trade.exit_reason
                if reason.startswith("DCA"):
                    live_markers.append({
                        "time": exit_t or t,
                        "position": "belowBar" if trade.side == "LONG" else "aboveBar",
                        "color": "#00ff88",  # bright green for live
                        "shape": "arrowUp" if trade.side == "LONG" else "arrowDown",
                        "text": f"L:{reason}",
                        "price": trade.entry_price,
                    })
                elif reason == "TP":
                    live_markers.append({
                        "time": exit_t,
                        "position": "aboveBar" if trade.side == "LONG" else "belowBar",
                        "color": "#00ccff",  # bright cyan for live TP
                        "shape": "circle",
                        "text": f"L:TP +{trade.pnl_usdt:.2f}",
                        "price": trade.exit_price,
                    })
                elif reason in ("REVERSAL", "REVERSAL_CLOSE"):
                    live_markers.append({
                        "time": exit_t,
                        "position": "aboveBar",
                        "color": "#ffaa00",  # bright amber
                        "shape": "square",
                        "text": f"L:REV {trade.pnl_usdt:+.2f}",
                        "price": trade.exit_price,
                    })
                else:
                    live_markers.append({
                        "time": exit_t,
                        "position": "aboveBar" if trade.side == "LONG" else "belowBar",
                        "color": "#ff4466",  # bright red
                        "shape": "square",
                        "text": f"L:{reason} {trade.pnl_usdt:+.2f}",
                        "price": trade.exit_price,
                    })

            # Live markers from signal log — REMOVED
            # executor.trades already generates correct markers with proper timestamps.
            # signal_log markers used int(time.time()) causing all fills to share one timestamp.

            # Live entry marker for open positions
            for key, pos in executor.positions.items():
                if pos.condition == 0.0 or pos.symbol != symbol:
                    continue
                entry_t = pos.entry_time // 1000 if pos.entry_time > 0 else 0
                live_markers.append({
                    "time": entry_t,
                    "position": "belowBar" if pos.side == "LONG" else "aboveBar",
                    "color": "#00ff88" if pos.side == "LONG" else "#ff4466",
                    "shape": "arrowUp" if pos.side == "LONG" else "arrowDown",
                    "text": f"LIVE {pos.side}",
                    "price": pos.initial_entry_price,
                })

    # Sort markers by time
    markers.sort(key=lambda m: m["time"])

    # Config flags for frontend legend visibility
    pct_cfg = cfg["trading"].get("pct_hard_stop", {})
    config_flags = {
        "pct_stop_enabled": pct_cfg.get("enabled", False),
        "filters_enabled": any(
            f.get("enabled", False)
            for f in cfg["strategy"].get("timeframes", [{}])[0]
                .get("filters", {}).values()
            if isinstance(f, dict)
        ),
        "dyncomp_enabled": cfg["strategy"].get("dynamic_comp", {}).get("enabled", False),
    }

    # ── Live grid levels (DCA/TP/AVG lines from live executor) ──
    live_grid_levels = []
    if source == "live":
        executor_gl: LiveExecutor | None = _live_state.get("executor")
        if executor_gl:
            for key, lpos in executor_gl.positions.items():
                if lpos.condition == 0.0 or lpos.symbol != symbol:
                    continue
                # AVG entry
                live_grid_levels.append({
                    "price": lpos.average_entry_price,
                    "label": f"AVG ({lpos.dca_fills_count} DCA)",
                    "filled": True,
                })
                # Pending DCA
                if lpos.pending_dca_price > 0:
                    max_dca = cfg["trading"].get("max_dca_steps", 4)
                    if lpos.dca_fills_count < max_dca:
                        live_grid_levels.append({
                            "price": lpos.pending_dca_price,
                            "label": f"DCA{lpos.dca_fills_count + 1}",
                            "filled": False,
                        })
                # Pending TP
                if lpos.pending_tp_price > 0 and lpos.dca_fills_count > 0:
                    live_grid_levels.append({
                        "price": lpos.pending_tp_price,
                        "label": "TP",
                        "filled": False,
                    })
                # PCT hard stop
                pct_cfg_l = cfg["trading"].get("pct_hard_stop", {})
                if pct_cfg_l.get("enabled", False):
                    loss_pct_l = pct_cfg_l.get("loss_pct", 2.5) / 100
                    if lpos.side == "LONG":
                        stop_p = lpos.average_entry_price * (1 - loss_pct_l)
                    else:
                        stop_p = lpos.average_entry_price * (1 + loss_pct_l)
                    max_dca_l = cfg["trading"].get("max_dca_steps", 4)
                    live_grid_levels.append({
                        "price": stop_p,
                        "label": f"STOP {pct_cfg_l.get('loss_pct', 2.5)}%"
                                 + (" (aktif)" if lpos.dca_fills_count >= max_dca_l else ""),
                        "filled": lpos.dca_fills_count >= max_dca_l,
                    })

    # Use live grid levels if available, otherwise dry-run
    final_grid = live_grid_levels if live_grid_levels else (
        grid_levels if sim and pos and pos.condition != 0.0 else []
    )

    return {
        "symbol": symbol,
        "interval": chart_interval,
        "candles": candles,
        "markers": markers,
        "live_markers": live_markers,
        "grid_levels": final_grid,
        "config_flags": config_flags,
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


def _run_weekly_reset_backtest(symbols: list[str], days: int, config: dict, reset_period: str = "weekly") -> None:
    """Run periodic reset backtest — supports daily/weekly, multi-pair (capital split)."""
    try:
        from core.engine.fast_backtest import fetch_and_cache_klines, run_fast_backtest
        import copy
        from datetime import datetime, timezone, timedelta

        config = copy.deepcopy(config)
        trading = config.get("trading", {})
        total_balance = trading.get("initial_balance", 10000.0)
        num_pairs = len(symbols)
        per_pair_balance = total_balance / num_pairs
        margin = trading.get("margin_per_trade", 300.0)

        _fast_bt_state["progress"] = 5

        project_root = str(Path(__file__).resolve().parent.parent)
        cache_dir = str(Path(project_root) / "data")

        # Fetch data for all pairs
        pair_dfs = {}
        for idx, sym in enumerate(symbols):
            _fast_bt_state["progress"] = 5 + int(10 * idx / num_pairs)
            df = fetch_and_cache_klines(sym, "3m", days, cache_dir=cache_dir)
            df["open_time_ms"] = df["open_time"] if df["open_time"].iloc[0] > 1e12 else df["open_time"] * 1000
            pair_dfs[sym] = df
            logger.info("[WEEKLY_BT] Loaded %d bars for %s", len(df), sym)

        # Determine period boundaries from first pair
        ref_df = pair_dfs[symbols[0]]
        start_ts = ref_df["open_time_ms"].iloc[0]
        end_ts = ref_df["open_time_ms"].iloc[-1]
        period_days = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}.get(reset_period, 7)
        period_ms = period_days * 24 * 3600 * 1000
        period_label = {"daily": "gun", "weekly": "hafta", "biweekly": "2hafta", "monthly": "ay"}.get(reset_period, "hafta")

        week_ranges = []
        ws = start_ts
        while ws < end_ts:
            we = ws + period_ms
            week_ranges.append((ws, we))
            ws = we

        if not week_ranges:
            _fast_bt_state["error"] = "Yeterli veri yok"
            return

        logger.info("[WEEKLY_BT] %d %s, %d pairs, $%.0f total ($%.0f/pair), margin=$%.0f",
                    len(week_ranges), period_label, num_pairs, total_balance, per_pair_balance, margin)

        all_trades = []
        all_equity = []
        weekly_summary = []
        trade_id_offset = 0
        cumulative_profit = 0.0
        equity_base = 0.0

        for i, (w_start, w_end) in enumerate(week_ranges):
            _fast_bt_state["progress"] = 15 + int(80 * i / len(week_ranges))

            week_total_profit = 0.0
            week_total_trades = 0
            week_total_winning = 0
            week_total_losing = 0
            week_max_dd = 0.0
            pair_details = {}

            for sym in symbols:
                df = pair_dfs[sym]
                week_df = df[(df["open_time_ms"] >= w_start) & (df["open_time_ms"] < w_end)].reset_index(drop=True)
                min_bars = 50 if reset_period == "weekly" else 10
                if len(week_df) < min_bars:
                    pair_details[sym] = {"profit": 0, "wr": 0, "dd": 0, "trades": 0}
                    continue

                week_cfg = copy.deepcopy(config)
                week_cfg["trading"]["initial_balance"] = per_pair_balance
                # margin stays as configured — user sets it for per-pair

                result = run_fast_backtest(week_df, week_cfg, sym)
                m = result.metrics
                pair_profit = m["current_balance"] - per_pair_balance

                for t in result.trades:
                    t["id"] = t.get("id", 0) + trade_id_offset
                    t["symbol"] = sym
                    all_trades.append(t)
                trade_id_offset += len(result.trades)

                week_total_profit += pair_profit
                week_total_trades += m.get("total_trades", 0)
                week_total_winning += m.get("winning_trades", 0)
                week_total_losing += m.get("losing_trades", 0)
                week_max_dd = max(week_max_dd, m.get("max_drawdown_pct", 0))

                pair_details[sym] = {
                    "profit": round(pair_profit, 2),
                    "wr": round(m.get("win_rate", 0), 1),
                    "dd": round(m.get("max_drawdown_pct", 0), 1),
                    "trades": m.get("total_trades", 0),
                }

            # Equity curve point
            cumulative_profit += week_total_profit
            all_equity.append({
                "time": int(w_start),
                "balance": cumulative_profit,
            })
            equity_base = cumulative_profit

            week_start_dt = datetime.fromtimestamp(w_start / 1000, tz=timezone.utc)
            week_end_dt = datetime.fromtimestamp(w_end / 1000, tz=timezone.utc)
            week_pnl_pct = (week_total_profit / total_balance) * 100

            summary_entry = {
                "week": i + 1,
                "start": week_start_dt.strftime("%m/%d"),
                "end": week_end_dt.strftime("%m/%d"),
                "profit": round(week_total_profit, 2),
                "pnl_pct": round(week_pnl_pct, 1),
                "win_rate": round(week_total_winning / week_total_trades * 100, 1) if week_total_trades > 0 else 0,
                "max_dd": round(week_max_dd, 1),
                "trades": week_total_trades,
                "winning": week_total_winning,
                "losing": week_total_losing,
            }
            # Add per-pair breakdown
            for sym in symbols:
                short_sym = sym.replace("USDT", "")
                pd_entry = pair_details.get(sym, {})
                summary_entry[f"{short_sym}_profit"] = pd_entry.get("profit", 0)
                summary_entry[f"{short_sym}_wr"] = pd_entry.get("wr", 0)
            weekly_summary.append(summary_entry)

        # Aggregate metrics
        total_weeks = len(weekly_summary)
        winning_weeks = sum(1 for w in weekly_summary if w["profit"] > 0)
        total_profit = sum(w["profit"] for w in weekly_summary)
        avg_weekly_pnl = sum(w["pnl_pct"] for w in weekly_summary) / total_weeks if total_weeks > 0 else 0
        max_dd = max(w["max_dd"] for w in weekly_summary) if weekly_summary else 0
        total_trades = sum(w["trades"] for w in weekly_summary)
        total_winning = sum(w["winning"] for w in weekly_summary)
        total_losing = sum(w["losing"] for w in weekly_summary)

        best_week = max(weekly_summary, key=lambda w: w["pnl_pct"]) if weekly_summary else {}
        worst_week = min(weekly_summary, key=lambda w: w["pnl_pct"]) if weekly_summary else {}

        # Compute detailed metrics from all trades
        all_pnls = [t.get("pnl_usdt", 0) for t in all_trades]
        all_fees = [t.get("fee_usdt", 0) for t in all_trades]
        wins = [p for p in all_pnls if p > 0]
        losses = [p for p in all_pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999

        metrics = {
            "initial_balance": total_balance,
            "current_balance": total_balance + total_profit,
            "total_pnl": round(total_profit, 2),
            "total_pnl_pct": round((total_profit / total_balance) * 100, 1) if total_balance > 0 else 0,
            "max_drawdown_pct": max_dd,
            "max_drawdown_usdt": 0,
            "max_runup_pct": 0,
            "max_runup_usdt": 0,
            "total_trades": total_trades,
            "winning_trades": total_winning,
            "losing_trades": total_losing,
            "win_rate": round(total_winning / total_trades * 100, 1) if total_trades > 0 else 0,
            "profit_factor": round(pf, 2),
            "total_fees": round(sum(all_fees), 2),
            "maker_fees": 0,
            "taker_fees": 0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "best_trade_pnl": round(max(all_pnls), 2) if all_pnls else 0,
            "worst_trade_pnl": round(min(all_pnls), 2) if all_pnls else 0,
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
            "calmar_ratio": 0,
            "recovery_factor": 0,
            "expectancy": round(sum(all_pnls) / len(all_pnls), 2) if all_pnls else 0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "avg_duration_min": 0,
            "elapsed_seconds": 0,
            "leverage": trading.get("leverage", 25),
            # Weekly-specific
            "weekly_reset": True,
            "total_weeks": total_weeks,
            "winning_weeks": winning_weeks,
            "losing_weeks": total_weeks - winning_weeks,
            "weekly_win_rate": round(winning_weeks / total_weeks * 100, 1) if total_weeks > 0 else 0,
            "total_profit": round(total_profit, 2),
            "avg_weekly_profit": round(total_profit / total_weeks, 2) if total_weeks > 0 else 0,
            "avg_weekly_pnl_pct": round(avg_weekly_pnl, 1),
            "best_week": best_week,
            "worst_week": worst_week,
            # Multi-pair info
            "symbols": symbols,
            "num_pairs": num_pairs,
            "per_pair_balance": per_pair_balance,
            "reset_period": reset_period,
        }

        _fast_bt_state["progress"] = 100
        # Limit trades to last 5000 to prevent frontend crash
        capped_trades = all_trades[-5000:] if len(all_trades) > 5000 else all_trades
        _fast_bt_state["result"] = {
            "trades": capped_trades,
            "equity_curve": all_equity,
            "drawdown_curve": [],
            "metrics": metrics,
            "per_symbol": [],
            "weekly_summary": weekly_summary,
        }
        logger.info("[WEEKLY_BT] Done: %d %s, %d/%d winning, %d pairs, total profit=$%.0f",
                    total_weeks, period_label, winning_weeks, total_weeks, num_pairs, total_profit)
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error("Weekly reset backtest failed: %s", str(e)[:200])
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
    oos_only = body.get("oos_only", False)
    weekly_reset = body.get("weekly_reset", False)

    _fast_bt_state["running"] = True
    _fast_bt_state["progress"] = 0
    _fast_bt_state["result"] = None
    _fast_bt_state["error"] = None

    if weekly_reset:
        symbols = body.get("symbols", [symbol])
        if isinstance(symbols, str):
            symbols = [symbols]
        if not symbols:
            symbols = [symbol]
        reset_period = body.get("reset_period", "weekly")
        thread = threading.Thread(
            target=_run_weekly_reset_backtest,
            args=(symbols, days, state["config"], reset_period),
            daemon=True,
        )
    else:
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


@app.post("/api/backtest/fast/reset")
def fast_backtest_reset():
    """Reset fast backtest state so a new one can be started."""
    if _fast_bt_state["running"]:
        return {"error": "Fast backtest is still running"}
    _fast_bt_state["running"] = False
    _fast_bt_state["progress"] = 0
    _fast_bt_state["result"] = None
    _fast_bt_state["error"] = None
    return {"status": "reset"}


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


@app.get("/api/live/debug")
def live_debug():
    """Debug endpoint — raw executor internal state vs exchange."""
    executor: LiveExecutor | None = _live_state.get("executor")
    if not executor:
        return {"error": "No executor"}
    result = {}
    with _live_lock:
        for key, pos in executor.positions.items():
            if pos.condition == 0.0:
                continue
            result[key] = {
                "symbol": pos.symbol,
                "side": pos.side,
                "condition": pos.condition,
                "dca_fills_count": pos.dca_fills_count,
                "dca_wave_sold": getattr(pos, "dca_wave_sold", "N/A"),
                "total_fills": getattr(pos, "total_fills", "N/A"),
                "remaining_qty": pos.remaining_qty,
                "average_entry_price": pos.average_entry_price,
                "initial_entry_price": getattr(pos, "initial_entry_price", "N/A"),
                "total_position_notional": getattr(pos, "total_position_notional", "N/A"),
                "margin_per_step": pos.margin_per_step,
                "pending_dca_order_id": pos.pending_dca_order_id,
                "pending_dca_price": pos.pending_dca_price,
                "tp_order_id": getattr(pos, "tp_order_id", 0),
                "pending_tp_price": pos.pending_tp_price,
                "hard_stop_price": pos.hard_stop_price,
                "entry_atr": pos.entry_atr,
                "_position_qty": executor._position_qty.get(key, "MISSING"),
                "_sl_order_id": executor._sl_order_ids.get(key, 0),
            }
    # WS + system health
    result["_system"] = {
        "ws_connected": executor._ws_connected,
        "ws_last_event_ts": executor._ws_last_event_ts,
        "ws_last_event_ago_s": round(time.time() - executor._ws_last_event_ts, 1) if executor._ws_last_event_ts > 0 else -1,
        "processed_order_ids_count": len(executor._processed_order_ids),
        "order_id_map_count": len(executor._order_id_map),
        "fill_log": executor._fill_log[-10:],
        "circuit_breaker": executor.circuit_breaker_triggered,
        "circuit_breaker_reason": executor.circuit_breaker_reason,
    }
    return result


@app.get("/api/live/order-history")
def live_order_history():
    """Get full order history — market entries, DCA fills, TP fills, pending orders."""
    api_key = _live_state.get("api_key", "")
    api_secret = _live_state.get("api_secret", "")
    if not api_key:
        return {"error": "API keys not configured"}

    executor = _live_state.get("executor")
    try:
        client = BinanceFutures(api_key, api_secret, testnet=_live_state.get("testnet", False))
        result = {"market_entries": [], "dca_orders": [], "tp_orders": [], "other_orders": []}

        for sym in _live_state.get("active_symbols", []):
            try:
                all_orders = client.get_all_orders(sym, limit=200)
                for o in all_orders:
                    order_type = o.get("type", "")
                    reduce_only = o.get("reduceOnly", False)
                    status = o.get("status", "")
                    side = o.get("side", "")

                    order_info = {
                        "orderId": o.get("orderId"),
                        "symbol": o.get("symbol"),
                        "side": side,
                        "type": order_type,
                        "price": float(o.get("price", 0)),
                        "avgPrice": float(o.get("avgPrice", 0)),
                        "origQty": float(o.get("origQty", 0)),
                        "executedQty": float(o.get("executedQty", 0)),
                        "status": status,
                        "reduceOnly": reduce_only,
                        "time": o.get("time"),
                        "updateTime": o.get("updateTime"),
                    }

                    if order_type == "MARKET":
                        order_info["role"] = "ENTRY"
                        result["market_entries"].append(order_info)
                    elif order_type == "LIMIT" and reduce_only:
                        order_info["role"] = "TP"
                        result["tp_orders"].append(order_info)
                    elif order_type == "LIMIT" and not reduce_only:
                        order_info["role"] = "DCA"
                        result["dca_orders"].append(order_info)
                    else:
                        order_info["role"] = order_type
                        result["other_orders"].append(order_info)
            except Exception:
                pass

        # Add fill log from executor
        if executor:
            with _live_lock:
                result["fill_log"] = list(executor._fill_log)
                result["trades"] = [
                    {
                        "id": t.id, "symbol": t.symbol, "side": t.side,
                        "entry_price": t.entry_price, "exit_price": t.exit_price,
                        "exit_reason": t.exit_reason, "qty": t.qty,
                        "pnl_usdt": t.pnl_usdt, "pnl_percent": t.pnl_percent,
                        "fee_usdt": t.fee_usdt,
                    }
                    for t in executor.trades
                ]

        return result
    except Exception as e:
        return {"error": str(e)[:200]}


@app.get("/api/live/orders")
def live_open_orders():
    """Get open limit orders + positions from Binance — real exchange state."""
    api_key = _live_state.get("api_key", "")
    api_secret = _live_state.get("api_secret", "")
    if not api_key:
        return {"error": "API keys not configured"}

    try:
        client = BinanceFutures(api_key, api_secret, testnet=_live_state.get("testnet", False))
        positions = client.get_positions()

        # Get open orders for all active symbols
        orders = []
        for sym in _live_state.get("active_symbols", []):
            try:
                sym_orders = client.get_open_orders(sym)
                for o in sym_orders:
                    orders.append({
                        "orderId": o.get("orderId"),
                        "symbol": o.get("symbol"),
                        "side": o.get("side"),
                        "type": o.get("type"),
                        "price": float(o.get("price", 0)),
                        "origQty": float(o.get("origQty", 0)),
                        "executedQty": float(o.get("executedQty", 0)),
                        "status": o.get("status"),
                        "reduceOnly": o.get("reduceOnly"),
                        "time": o.get("time"),
                    })
            except Exception:
                pass

        return {"positions": positions, "orders": orders}
    except Exception as e:
        return {"error": str(e)[:200]}


def _live_signal_scanner_loop() -> None:
    """Background thread: signal scanner for live trading mode.

    Same logic as dry-run scanner, but uses LiveExecutor instead of Simulator.
    Thread is protected against ALL exceptions — will never die while running=True.
    """
    logger.info("Live signal scanner started")
    last_scan_bucket = 0
    _consecutive_errors = 0

    while _live_state["running"]:
      try:
        # Event-driven: wait for WS candle close event OR fallback poll every 5s
        triggered = _candle_close_event.wait(timeout=5)
        _candle_close_event.clear()

        if not _live_state["running"] or not _live_state["active_symbols"]:
            continue

        cfg = state["config"]
        tf_configs = cfg["strategy"].get("timeframes", [])
        tf = tf_configs[0].get("timeframe", "3m") if tf_configs else cfg["strategy"].get("timeframe", "3m")
        tf_label = tf_configs[0].get("label", tf) if tf_configs else tf
        tf_cfg = tf_configs[0] if tf_configs else None
        interval_s = _TIMEFRAME_SECONDS.get(tf, 900)

        now_ts = int(time.time())
        current_bucket = now_ts // interval_s
        if current_bucket == last_scan_bucket:
            continue
        last_scan_bucket = current_bucket
        if not triggered:
            time.sleep(1)  # fallback poll: small wait for finalization

        logger.info("Live scanner: new %s candle — rescanning %d symbols",
                     tf, len(_live_state["active_symbols"]))

        executor: LiveExecutor | None = _live_state["executor"]
        if not executor:
            continue

        # ── Position Sync — reconcile with exchange every ~60s ──
        try:
            with _live_lock:
                # Truncate signal_log to prevent memory leak
                if len(_live_state["signal_log"]) > 500:
                    _live_state["signal_log"] = _live_state["signal_log"][-200:]

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
        except Exception as e:
            logger.error("[SCANNER] Sync/CB error: %s", str(e)[:200])

        rest: BinanceRest = _live_state.get("rest", state["rest"])

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

                # Use tf_config for correct PMax parameters
                engine = SignalEngine(cfg, tf_config=tf_cfg)
                signal = engine.process(df)

                # Position key includes tf_label (e.g. "BARDUSDT:3m")
                pos_key = f"{sym}:{tf_label}" if tf_label else sym

                with _live_lock:
                    # ── STEP 1: process_candle FIRST — detect fills + stop checks ──
                    # CRITICAL: Must run BEFORE KC update to detect filled orders
                    # before KC update replaces them with new order IDs
                    if executor.has_position(sym, tf_label):
                        candle_close = float(last_closed["close"])
                        close_time = int(last_closed.get("close_time", 0))
                        candle_high = float(last_closed["high"])
                        candle_low = float(last_closed["low"])
                        from core.strategy.indicators import atr as atr_indicator
                        dyn_sl_period = cfg["trading"].get("dynamic_sl", {}).get("atr_period", 12)
                        _current_atr = 0.0
                        if len(df) > dyn_sl_period:
                            _atr_s = atr_indicator(df["high"], df["low"], df["close"], dyn_sl_period)
                            _current_atr = float(_atr_s.iloc[-1]) if not pd.isna(_atr_s.iloc[-1]) else 0.0
                        exit_trades = executor.process_candle(sym, candle_high, candle_low, close_time, tf_label=tf_label, candle_close=candle_close, current_atr=_current_atr)
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
                        if not executor.has_position(sym, tf_label):
                            _live_state["scan_results"][sym] = {
                                "status": "closed_tp",
                                "side": _live_state["scan_results"].get(sym, {}).get("side", ""),
                                "price": _live_state["scan_results"].get(sym, {}).get("price", 0),
                                "last_price": float(last_closed["close"]),
                            }

                    # ── STEP 2: KC update AFTER fill detection ──
                    # Update DCA + TP prices from latest KC bands and replace orders
                    # This runs AFTER process_candle so filled orders are already processed
                    if executor.has_position(sym, tf_label):
                        pos_update = executor.positions.get(pos_key)
                        if pos_update and pos_update.condition != 0.0:
                            try:
                                from core.strategy.indicators import keltner_channel as calc_kc_update
                                kc_cfg_u = (tf_cfg or {}).get("keltner", {})
                                _, kc_u_upd, kc_l_upd = calc_kc_update(
                                    df["high"], df["low"], df["close"],
                                    kc_length=kc_cfg_u.get("length", 3),
                                    kc_multiplier=kc_cfg_u.get("multiplier", 0.5),
                                    atr_period=kc_cfg_u.get("atr_period", 2),
                                )
                                if pos_update.side == "LONG":
                                    new_tp = float(kc_u_upd.iloc[-1])
                                    new_dca = float(kc_l_upd.iloc[-1])
                                else:
                                    new_tp = float(kc_l_upd.iloc[-1])
                                    new_dca = float(kc_u_upd.iloc[-1])
                                pos_update.pending_tp_price = new_tp
                                pos_update.pending_dca_price = new_dca
                                executor._place_tp_order(pos_key, pos_update)
                                executor._place_dca_orders(pos_key, pos_update)
                            except Exception as e:
                                logger.error("[KC_UPDATE] %s: %s", sym, str(e)[:100])

                    # ── STEP 2.5: Re-entry after graduated TP full close ──
                    reentry_symbols = executor.pop_reentry_queue()
                    for re_sym in reentry_symbols:
                        if re_sym != sym:
                            continue
                        if executor.has_position(re_sym, tf_label):
                            continue  # already re-entered
                        if executor.circuit_breaker_triggered:
                            continue
                        # Use backfill to find current PMax direction
                        re_engine = SignalEngine(cfg, tf_config=tf_cfg)
                        re_signal = re_engine.process_backfill(df)
                        if re_signal:
                            re_signal.tf_label = tf_label
                            try:
                                executor.refresh_balance()
                                re_trades = executor.process_signal(re_signal, entry_time=int(time.time() * 1000))
                                logger.info("[REENTRY] %s — re-entered %s @ %.4f after TP full close",
                                            re_sym, re_signal.side, re_signal.price)
                                _live_state["signal_log"].append({
                                    "time": time.strftime("%H:%M:%S"),
                                    "symbol": re_sym,
                                    "side": re_signal.side,
                                    "price": re_signal.price,
                                    "rsi": round(re_signal.rsi_value, 2),
                                    "source": "REENTRY_AFTER_TP_CLOSE",
                                })
                            except Exception as e:
                                logger.error("[REENTRY] %s failed: %s", re_sym, str(e)[:100])

                    if signal:
                        # Set tf_label on signal so executor creates correct pos key
                        signal.tf_label = tf_label

                        has_pos = executor.has_position(sym, tf_label)
                        if has_pos:
                            existing = executor.positions.get(pos_key)
                            if existing and existing.side == signal.side:
                                continue

                        # Refresh balance BEFORE acquiring lock for process_signal
                        try:
                            executor.refresh_balance()
                        except Exception:
                            pass

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

                        pos = executor.positions.get(pos_key)
                        if pos and pos.condition != 0.0:
                            logger.info(
                                "[LIVE_ENTRY] %s %s [%s] @ %.4f",
                                sym, signal.side, tf_label, signal.price,
                            )
                            # Backtest parity: entry candle'da KC set edilmiyor,
                            # DCA/TP order konulmuyor. Sonraki candle KC update yapacak.
                            # (Rust engine: pending_dca=0, pending_tp=0 at entry)

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

      except Exception as e:
        _consecutive_errors += 1
        if _consecutive_errors >= 5:
            logger.critical("[SCANNER] %d consecutive errors — last: %s", _consecutive_errors, str(e)[:200])
        else:
            logger.error("[SCANNER] Error in scanner loop (will retry, count=%d): %s", _consecutive_errors, str(e)[:200])
        import traceback
        traceback.print_exc()
        # Exponential backoff: 5s, 10s, 20s, 30s max
        backoff = min(30, 5 * (2 ** min(_consecutive_errors - 1, 3)))
        time.sleep(backoff)
      else:
        _consecutive_errors = 0  # Reset on successful iteration

    logger.info("Live signal scanner stopped")


async def _on_live_book_ticker(ticker: dict) -> None:
    """Update live mode book ticker cache."""
    with _live_ws_book_lock:
        _live_ws_book_data[ticker["symbol"]] = ticker


# ── Event-driven candle close trigger ──
_candle_close_event = threading.Event()

async def _on_live_candle(candle: dict) -> None:
    """WS kline event — trigger scanner immediately on candle close."""
    if candle.get("is_closed"):
        _candle_close_event.set()
        logger.info("[WS_KLINE] %s %s candle closed — triggering immediate scan",
                    candle["symbol"], candle["interval"])


def _start_live_ws_loop(symbols: list[str]) -> None:
    """Start WS bookTicker + User Data Stream for live mode."""
    global _live_ws_instance
    loop = asyncio.new_event_loop()

    async def _on_order_update(event: dict) -> None:
        """ORDER_TRADE_UPDATE handler — real-time fill detection via WS.
        Wrapped in try/except to prevent ANY exception from killing WS listener.
        """
        try:
            data = event.get("o", {})
            exec_type = data.get("X", "")

            if exec_type == "PARTIALLY_FILLED":
                logger.info("[WS_PARTIAL] %s orderId=%s filled=%.6f/%.6f",
                            data.get("s"), data.get("i"),
                            float(data.get("z", 0) or 0), float(data.get("q", 0) or 0))
                return

            if exec_type != "FILLED":
                return

            order_data = {
                "orderId": int(data.get("i", 0) or 0),
                "symbol": data.get("s", ""),
                "side": data.get("S", ""),
                "avgPrice": float(data.get("ap", 0) or 0),
                "executedQty": float(data.get("z", 0) or 0),
                "cumQuote": float(data.get("Z", 0) or 0),
                "orderType": data.get("o", ""),
                "reduceOnly": data.get("R", False),
            }

            if order_data["orderId"] <= 0:
                logger.warning("[WS] Event missing orderId, skipping")
                return

            with _live_lock:
                executor = _live_state.get("executor")
                if not executor:
                    return
                executor._ws_connected = True

                # Log DCA fill to signal_log for chart markers
                oid = order_data["orderId"]
                mapping = executor._order_id_map.get(oid)
                fill_type = mapping[1] if (mapping and len(mapping) >= 2) else None
                fill_symbol = order_data.get("symbol", "")
                fill_price = order_data.get("avgPrice", 0)

                if fill_type == "DCA" and oid not in executor._processed_order_ids:
                    pos_ref = executor._positions.get(mapping[0])
                    dc_before = pos_ref.dca_fills_count if pos_ref else 0
                    _live_state["signal_log"].append({
                        "time": time.strftime("%H:%M:%S"),
                        "symbol": fill_symbol,
                        "side": pos_ref.side if pos_ref else "LONG",
                        "price": fill_price,
                        "rsi": 0,
                        "source": f"LIVE_DCA{dc_before + 1}_FILL",
                    })

            trades = executor.process_order_fill(order_data)
            for t in trades:
                _live_state["signal_log"].append({
                    "time": time.strftime("%H:%M:%S"),
                    "symbol": t.symbol,
                    "side": t.side,
                    "price": t.exit_price,
                    "rsi": 0,
                    "source": f"WS_{t.exit_reason}_FILL +${t.pnl_usdt:.2f}",
                })
        except Exception as e:
            logger.error("[WS_CALLBACK] Exception in _on_order_update (WS listener preserved): %s", str(e)[:200])

    async def _on_uds_reconnect() -> None:
        """Called when UDS WS reconnects — catch up missed fills."""
        with _live_lock:
            executor = _live_state.get("executor")
            if executor:
                executor._catchup_poll_all_orders()
                logger.info("[UDS] Reconnect catch-up completed")

    async def _run():
        global _live_ws_instance
        is_testnet = _live_state.get("testnet", False)

        # 1. Market data WS (bookTicker + kline for candle close trigger)
        ws = BinanceWS(on_candle=_on_live_candle, on_book_ticker=_on_live_book_ticker, testnet=is_testnet)
        _live_ws_instance = ws
        await ws.connect()
        tf_ws = tf_configs[0].get("timeframe", "3m") if tf_configs else "3m"
        for sym in symbols:
            await ws.subscribe_book_ticker(sym)
            await ws.subscribe(sym, tf_ws)  # kline stream for candle close detection
        logger.info("Live WS bookTicker + kline(%s) subscribed for %d symbols", tf_ws, len(symbols))

        # 2. User Data Stream WS (order fills) — DUAL redundant connections
        uds_ws = None
        listen_key = ""
        client = _live_state.get("client")
        if client:
            try:
                listen_key = client.create_listen_key()
                uds_ws = DualUserDataWS(
                    on_order_update=_on_order_update,
                    on_reconnect=_on_uds_reconnect,
                    testnet=is_testnet,
                )
                await uds_ws.connect(listen_key)
                logger.info("[UDS] Dual User Data Stream started (2 redundant connections)")

                # Store UDS ref for executor WS health tracking + frontend status
                with _live_lock:
                    executor = _live_state.get("executor")
                    if executor:
                        executor._ws_connected = True
                    _live_state["_uds_ws"] = uds_ws
            except Exception as e:
                logger.error("[UDS] Failed to start Dual User Data Stream: %s", e)
                uds_ws = None

        # 3. ListenKey renewal (every 25 min) + 24h reconnect
        async def _renew_loop():
            nonlocal listen_key, uds_ws
            while _live_state["running"]:
                await asyncio.sleep(25 * 60)
                if not client or not listen_key:
                    continue
                try:
                    client.renew_listen_key(listen_key)
                    logger.info("[UDS] Listen key renewed")
                except Exception as e:
                    logger.error("[UDS] Listen key renewal failed: %s — creating new key", e)
                    try:
                        listen_key = client.create_listen_key()
                        if uds_ws:
                            await uds_ws.reconnect(listen_key)
                    except Exception:
                        pass

        async def _daily_reconnect():
            nonlocal listen_key, uds_ws
            while _live_state["running"]:
                await asyncio.sleep(23 * 3600)  # 23h (1h margin before 24h disconnect)
                if not client:
                    continue
                logger.info("[UDS] 24h proactive reconnect")
                try:
                    listen_key = client.create_listen_key()
                    if uds_ws:
                        await uds_ws.reconnect(listen_key)
                    logger.info("[UDS] 24h reconnect successful")
                except Exception as e:
                    logger.error("[UDS] 24h reconnect failed: %s", e)

        if uds_ws:
            asyncio.create_task(_renew_loop())
            asyncio.create_task(_daily_reconnect())

        # 3b. REST Heartbeat — verify pending orders every 3s (fast fill detection)
        async def _rest_heartbeat_loop():
            while _live_state["running"]:
                await asyncio.sleep(3)
                if not _live_state["running"]:
                    break
                with _live_lock:
                    executor = _live_state.get("executor")
                    if executor:
                        try:
                            msgs = executor.rest_heartbeat()
                            for m in msgs:
                                _live_state["signal_log"].append({
                                    "time": time.strftime("%H:%M:%S"),
                                    "symbol": "SYSTEM",
                                    "side": "SYNC",
                                    "price": 0,
                                    "rsi": 0,
                                    "source": m,
                                })
                        except Exception as e:
                            logger.error("[REST_HEARTBEAT] Error: %s", e)

        asyncio.create_task(_rest_heartbeat_loop())

        # 4. Main loop — wait until live stopped
        while _live_state["running"]:
            await asyncio.sleep(1)

        # Cleanup
        try:
            await ws.close()
        except Exception:
            pass
        if uds_ws:
            try:
                await uds_ws.close()
            except Exception:
                pass
        if client and listen_key:
            try:
                client.close_listen_key(listen_key)
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
    is_testnet = _live_state.get("testnet", False)
    rest = BinanceRest(testnet=is_testnet)  # testnet ise testnet REST kullan
    _live_state["rest"] = rest  # scanner'da da kullanılacak
    tf_configs = cfg["strategy"].get("timeframes", [])
    tf_cfg = tf_configs[0] if tf_configs else None
    tf_label = (tf_cfg or {}).get("label", "3m")

    for sym in symbols:
        try:
            tf_configs = cfg["strategy"].get("timeframes", [])
            interval = tf_configs[0].get("timeframe", "3m") if tf_configs else cfg["strategy"].get("timeframe", "3m")
            klines = rest.fetch_klines_sync(sym, interval, limit=1500)
            if len(klines) > 1:
                klines = klines[:-1]
            if len(klines) < 200:
                scan_results[sym] = {"status": "insufficient_data", "candles": len(klines)}
                continue

            df = pd.DataFrame(klines)
            df["symbol"] = sym

            tf_cfg = tf_configs[0] if tf_configs else None
            engine = SignalEngine(cfg, tf_config=tf_cfg)
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
                # Set tf_label on signal
                signal.tf_label = tf_label

                # Mevcut trende aninda giris yap (dry-run gibi)
                try:
                    trades = executor.process_signal(signal, entry_time=int(time.time() * 1000))
                    pair_state = "ACTIVE" if executor.is_active(sym) else "OBSERVING"
                    logger.info(
                        "[LIVE_INIT] %s — immediate entry %s @ %.4f (pair_state=%s)",
                        sym, signal.side, signal.price, pair_state,
                    )

                    # Backtest parity: entry candle'da KC set edilmiyor,
                    # DCA/TP order konulmuyor. Sonraki candle scanner KC update yapacak.
                    # (Rust engine: pending_dca=0, pending_tp=0 at entry)

                except Exception as e:
                    logger.error("[LIVE_INIT] %s — immediate entry failed: %s", sym, e)
                    pair_state = "OBSERVING"

                scan_results[sym] = {
                    "status": "signal_detected",
                    "side": signal.side,
                    "price": signal.price,
                    "rsi": round(signal.rsi_value, 2),
                    "atr": round(signal.atr_value, 4),
                    "last_price": last_price,
                    "pair_state": pair_state,
                }
                signal_log.append({
                    "time": time.strftime("%H:%M:%S"),
                    "symbol": sym,
                    "side": signal.side,
                    "price": signal.price,
                    "rsi": round(signal.rsi_value, 2),
                    "source": "IMMEDIATE_ENTRY",
                })
            else:
                from core.strategy.indicators import pmax as calc_pmax, rsi as calc_rsi
                pmax_cfg = (tf_cfg or {}).get("pmax", cfg["strategy"].get("pmax", {}))
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

    # Persist recovery info to disk (API keys NOT saved for security)
    try:
        recovery = {
            "active_symbols": symbols,
            "pair_configs": pair_configs,
            "testnet": is_testnet,
            "started_at": time.time(),
        }
        recovery_path = Path(__file__).parent.parent / "config" / "live_recovery.json"
        with open(recovery_path, "w") as f:
            json.dump(recovery, f)
        logger.info("[RECOVERY] Live state saved to %s", recovery_path)
    except Exception as e:
        logger.error("[RECOVERY] Failed to save: %s", e)

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

    # Remove recovery file
    try:
        recovery_path = Path(__file__).parent.parent / "config" / "live_recovery.json"
        if recovery_path.exists():
            recovery_path.unlink()
    except Exception:
        pass

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

    # Positions — fetch REAL data from Binance exchange (not bot's internal tracking)
    positions = []
    exchange_positions_map: dict[str, dict] = {}
    with _live_lock:
        scan_results_snapshot = dict(_live_state.get("scan_results", {}))
        signal_log_snapshot = list(_live_state.get("signal_log", []))[-50:]
        stats = executor.get_stats()
        client = _live_state.get("client")

    # Fetch real positions from Binance — this is the source of truth
    if client:
        try:
            real_positions = client.get_positions()
            for rp in real_positions:
                exchange_positions_map[rp["symbol"]] = rp
        except Exception as e:
            logger.error("[STATUS] Failed to fetch exchange positions: %s", e)

    for sym in symbols:
        rp = exchange_positions_map.get(sym)
        if not rp:
            continue

        pc = _live_state["pair_configs"].get(sym, {})

        # All values from Binance — no bot-side calculation
        ob = orderbook.get(sym, {})
        bid = ob.get("bid", rp["mark_price"])
        ask = ob.get("ask", rp["mark_price"])
        spread = ask - bid if ob else 0

        notional = rp["notional"]
        leverage = rp["leverage"]
        used_margin = notional / leverage if leverage > 0 else float(pc.get("margin", 100))
        upnl_usdt = rp["unrealized_pnl"]
        upnl_pct = (upnl_usdt / used_margin * 100) if used_margin > 0 else 0

        realized = sum(t.pnl_usdt for t in executor.trades if t.symbol == sym)
        fees = sum(t.fee_usdt for t in executor.trades if t.symbol == sym)

        positions.append({
            "symbol": sym,
            "side": rp["side"],
            "entry_price": round(rp["entry_price"], 6),
            "mark_price": round(rp["mark_price"], 6),
            "bid": round(bid, 6),
            "ask": round(ask, 6),
            "spread": round(spread, 8),
            "break_even": round(rp.get("break_even_price", rp["entry_price"]), 6),
            "notional_usdt": round(notional, 2),
            "condition": -1.0 if rp["side"] == "SHORT" else 1.0,
            "remaining_qty": 1.0,
            "unrealized_pnl_usdt": round(upnl_usdt, 4),
            "unrealized_pnl_pct": round(upnl_pct, 4),
            "realized_pnl_usdt": round(realized, 4),
            "total_pnl_usdt": round(upnl_usdt + realized, 4),
            "fees_usdt": round(fees, 4),
            "pair_state": executor.get_pair_state(sym),
            "margin": round(used_margin, 2),
            "leverage": leverage,
            "qty": rp["amount"],
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

        ob = orderbook.get(sym, {})  # sym here is base symbol from active_symbols
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

    # Balance ve PnL: Binance'den gerçek veri çek
    live_balance = executor.balance
    live_available = executor.available_balance
    if client:
        try:
            bal_data = client.get_balance()
            live_balance = bal_data["balance"]
            live_available = bal_data["available"]
        except Exception:
            pass

    # PnL calculation — all from Binance
    # Binance wallet_balance = initial + realized_pnl (fees already deducted by Binance)
    # Binance wallet_balance does NOT include unrealized PnL
    initial_bal = executor._initial_balance or live_balance
    realized_pnl = live_balance - initial_bal  # fee dahil, Binance zaten düşmüş
    total_pnl = realized_pnl + total_unrealized  # realized + unrealized = total

    return {
        "live_running": _live_state["running"],
        "balance": round(live_balance, 2),
        "available": round(live_available, 2),
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
            "realized_pnl": round(realized_pnl, 4),
            "total_pnl": round(total_pnl, 4),
            "total_fees": round(stats.get("total_fees", 0), 4),
            "net_pnl": round(total_pnl, 4),
        },
        "pair_configs": _live_state.get("pair_configs", {}),
        "ws_health": _build_ws_health(executor),
    }


def _build_ws_health(executor) -> dict:
    """Build WS health metrics for frontend display."""
    now = time.time()
    ws_last = executor._ws_last_event_ts if executor else 0
    ws_connected = executor._ws_connected if executor else False
    silent_ms = int((now - ws_last) * 1000) if ws_last > 0 else -1

    # Order state machine stats
    pending_orders = []
    total_tracked = 0
    mismatches = 0
    if executor and hasattr(executor, "_order_states"):
        total_tracked = len(executor._order_states)
        mismatches = sum(1 for o in executor._order_states.values() if o.mismatch)
        for o in executor._order_states.values():
            if o.status.value == "ACK":
                pending_orders.append({
                    "order_id": o.order_id,
                    "symbol": o.symbol,
                    "type": o.order_type,
                    "side": o.side,
                    "price": round(o.price, 4),
                    "age_s": round(now - o.acked_at, 1),
                    "last_verify_s": round(now - o.last_rest_verify, 1) if o.last_rest_verify > 0 else -1,
                })

    # UDS connection status
    uds_connected = False
    live_ws = _live_state.get("_uds_ws")
    if live_ws and hasattr(live_ws, "connected"):
        uds_connected = live_ws.connected

    # Heartbeat stats
    hb_last = executor._last_rest_heartbeat if executor and hasattr(executor, "_last_rest_heartbeat") else 0
    hb_age_ms = int((now - hb_last) * 1000) if hb_last > 0 else -1

    return {
        "uds_connected": uds_connected or ws_connected,
        "ws_last_event_ms": silent_ms,
        "ws_status": "LIVE" if (ws_connected and silent_ms < 30000) else
                     "STALE" if (ws_connected and silent_ms >= 30000) else
                     "DISCONNECTED",
        "heartbeat_age_ms": hb_age_ms,
        "pending_orders": pending_orders,
        "total_tracked_orders": total_tracked,
        "mismatches": mismatches,
        "watchdog_timeout_s": 30 if len(pending_orders) <= 5 else 60 if len(pending_orders) <= 20 else 120,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ── SigmaKapital Monitor — Read-Only External Access ──
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/monitor/token")
async def create_monitor_token():
    """Yeni monitor token oluştur."""
    token = secrets.token_urlsafe(32)
    _monitor_tokens[token] = {
        "created_at": time.time(),
    }
    return {"token": token, "url": f"/sigmakapital/{token}"}


@app.get("/api/monitor/tokens")
async def list_monitor_tokens():
    """Aktif monitor tokenlerini listele."""
    return {
        "tokens": [
            {"token": t, "created_at": info["created_at"]}
            for t, info in _monitor_tokens.items()
        ]
    }


@app.delete("/api/monitor/token/{token}")
async def delete_monitor_token(token: str):
    """Monitor tokenı sil."""
    if token in _monitor_tokens:
        del _monitor_tokens[token]
        return {"ok": True}
    return {"error": "Token bulunamadı"}


@app.get("/api/sigmakapital")
async def monitor_data():
    """Monitor sayfası için read-only live data — public, token yok."""
    try:
        # Live çalışmıyorsa boş data dön
        if not _live_state.get("running"):
            return {
                "live_running": False,
                "balance": 0,
                "totals": {"unrealized_pnl": 0, "realized_pnl": 0, "total_pnl": 0, "total_fees": 0, "net_pnl": 0},
                "positions": [],
                "pair_summaries": {},
                "active_symbols": [],
                "stats": {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "win_rate": 0, "total_fees": 0},
                "trade_log": [],
            }

        # Live çalışıyor — gerçek datayı topla (live/status ile aynı pattern)
        executor: LiveExecutor | None = _live_state.get("executor")
        client: BinanceFutures | None = _live_state.get("client")
        if not executor:
            return {"error": "Executor bulunamadı", "code": 500}

        symbols = _live_state.get("active_symbols", [])

        # Stats
        with _live_lock:
            scan_results_snapshot = dict(_live_state.get("scan_results", {}))
            stats_raw = executor.get_stats()

        total_trades = stats_raw.get("total_trades", 0)
        winning = stats_raw.get("winning_trades", 0)
        losing = stats_raw.get("losing_trades", 0)
        total_fees_val = stats_raw.get("total_fees", 0)
        stats = {
            "total_trades": total_trades,
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": round(stats_raw.get("win_rate", 0), 1),
            "total_fees": round(total_fees_val, 4),
        }

        # Positions — Binance'den gerçek veri
        positions = []
        exchange_positions_map: dict[str, dict] = {}
        if client:
            try:
                real_positions = client.get_positions()
                for rp in real_positions:
                    exchange_positions_map[rp["symbol"]] = rp
            except Exception:
                pass

        for sym in symbols:
            rp = exchange_positions_map.get(sym)
            if not rp:
                continue
            pc = _live_state["pair_configs"].get(sym, {})
            notional = rp["notional"]
            leverage = rp["leverage"]
            used_margin = notional / leverage if leverage > 0 else float(pc.get("margin", 100))
            upnl_usdt = rp["unrealized_pnl"]
            upnl_pct = (upnl_usdt / used_margin * 100) if used_margin > 0 else 0
            realized = sum(t.pnl_usdt for t in executor.trades if t.symbol == sym)
            fees = sum(t.fee_usdt for t in executor.trades if t.symbol == sym)

            positions.append({
                "symbol": sym,
                "side": rp["side"],
                "entry_price": round(rp["entry_price"], 6),
                "mark_price": round(rp["mark_price"], 6),
                "notional_usdt": round(notional, 2),
                "unrealized_pnl_usdt": round(upnl_usdt, 4),
                "unrealized_pnl_pct": round(upnl_pct, 2),
                "realized_pnl_usdt": round(realized, 4),
                "fees_usdt": round(fees, 4),
                "dca_count": 0,
            })

        # Pair summaries
        pair_summaries = {}
        for sym in symbols:
            sym_trades = [t for t in executor.trades if t.symbol == sym]
            sym_realized = sum(t.pnl_usdt for t in sym_trades)
            sym_fees = sum(t.fee_usdt for t in sym_trades)
            pos_match = next((p for p in positions if p["symbol"] == sym), None)
            sym_unrealized = pos_match["unrealized_pnl_usdt"] if pos_match else 0.0
            scan = scan_results_snapshot.get(sym, {})
            pair_summaries[sym] = {
                "last_price": round(pos_match["mark_price"], 6) if pos_match else 0,
                "status": scan.get("status", "waiting"),
                "side": scan.get("side", ""),
                "unrealized_pnl": round(sym_unrealized, 4),
                "realized_pnl": round(sym_realized, 4),
                "total_pnl": round(sym_unrealized + sym_realized, 4),
                "fees": round(sym_fees, 4),
                "trade_count": len(sym_trades),
                "pair_state": executor.get_pair_state(sym),
            }

        total_unrealized = sum(p["unrealized_pnl_usdt"] for p in positions)

        # Balance
        live_balance = executor.balance
        live_available = executor.available_balance
        if client:
            try:
                bal_data = client.get_balance()
                live_balance = bal_data["balance"]
                live_available = bal_data["available"]
            except Exception:
                pass

        initial_bal = executor._initial_balance or live_balance
        realized_pnl = live_balance - initial_bal
        total_pnl = realized_pnl + total_unrealized

        return {
            "live_running": True,
            "balance": round(live_balance, 2),
            "available": round(live_available, 2),
            "active_symbols": symbols,
            "stats": stats,
            "positions": positions,
            "pair_summaries": pair_summaries,
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
                "realized_pnl": round(realized_pnl, 4),
                "total_pnl": round(total_pnl, 4),
                "total_fees": round(stats.get("total_fees", 0), 4),
                "net_pnl": round(total_pnl, 4),
            },
        }
    except Exception as e:
        logger.exception("monitor_data error")
        return {"error": str(e), "code": 500}
