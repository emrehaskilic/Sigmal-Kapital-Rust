"""Fast numpy-array backtest engine.

Pre-computes all indicators once, then walks signals in a single loop.
180 days (86,400 bars) completes in ~5 seconds vs ~3 hours for candle-by-candle.
With Rust engine (scalper_engine): ~0.05-0.1 seconds (50-100x faster).

Uses the same strategy logic as the optimizer:
  - Adaptive PMax (R3) for signal generation
  - Keltner Channel DCA/TP
  - Dynamic Compounding (balance-based tiers)
  - Dynamic Stop Loss (ATR-based, tightens on DCA full)
  - Hard Stop (5x ATR emergency backup)
  - EMA/RSI filters
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from core.strategy.indicators import (
    adaptive_pmax,
    atr,
    atr_rma,
    ema,
    keltner_channel,
    rsi,
)

logger = logging.getLogger(__name__)

MAX_CURVE_POINTS = 800  # max equity/drawdown points sent to frontend


def _downsample(data: list, max_points: int) -> list:
    """Uniform downsample keeping first and last points."""
    if len(data) <= max_points:
        return data
    step = (len(data) - 2) / (max_points - 2)
    out = [data[0]]
    for i in range(1, max_points - 1):
        out.append(data[round(i * step)])
    out.append(data[-1])
    return out


# --- Rust engine integration ---
try:
    import scalper_engine
    _USE_RUST = True
    logger.info("Rust engine (scalper_engine) loaded — fast backtest will use Rust")
except ImportError:
    _USE_RUST = False
    logger.info("Rust engine not available — using Python fallback")


@dataclass
class FastBacktestResult:
    trades: list[dict]
    equity_curve: list[dict]
    drawdown_curve: list[dict]
    metrics: dict
    per_symbol: list[dict]


def _get_source(df: pd.DataFrame, source: str = "hl2") -> pd.Series:
    if source == "hl2":
        return (df["high"] + df["low"]) / 2
    elif source == "hlc3":
        return (df["high"] + df["low"] + df["close"]) / 3
    elif source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    return df["close"]


def run_fast_backtest(
    df: pd.DataFrame,
    config: dict,
    symbol: str = "ETHUSDT",
) -> FastBacktestResult:
    """Run fast numpy-array backtest on a DataFrame of klines.

    df must have columns: open_time, open, high, low, close, volume
    Uses Rust engine if available, falls back to Python.
    """
    if _USE_RUST:
        return _run_rust_backtest(df, config, symbol)
    return _run_python_backtest(df, config, symbol)


def _run_rust_backtest(
    df: pd.DataFrame,
    config: dict,
    symbol: str = "ETHUSDT",
) -> FastBacktestResult:
    """Run backtest via Rust scalper_engine — 50-100x faster."""
    t_start = time.time()

    trading = config.get("trading", {})
    strategy = config.get("strategy", {})
    tf_configs = strategy.get("timeframes", [])
    tf_cfg = tf_configs[0] if tf_configs else {}

    pmax_cfg = tf_cfg.get("pmax", strategy.get("pmax", {}))
    pmax_source = pmax_cfg.get("source", "hl2")
    kc_cfg = tf_cfg.get("keltner", {})
    filters = tf_cfg.get("filters", {})
    ema_filter = filters.get("ema_trend", {})
    rsi_filter = filters.get("rsi", {})
    atr_vol_filter = filters.get("atr_volatility", {})
    hard_stop_cfg = trading.get("hard_stop", {})
    dyn_sl_cfg = trading.get("dynamic_sl", {})
    dyncomp = strategy.get("dynamic_comp", {})

    # Build source array
    src = _get_source(df, pmax_source)

    # Prepare numpy arrays (contiguous)
    src_arr = np.ascontiguousarray(src.values, dtype=np.float64)
    high_arr = np.ascontiguousarray(df["high"].values, dtype=np.float64)
    low_arr = np.ascontiguousarray(df["low"].values, dtype=np.float64)
    close_arr = np.ascontiguousarray(df["close"].values, dtype=np.float64)
    times_arr = np.ascontiguousarray(
        df["open_time"].values if "open_time" in df.columns else np.arange(len(df)),
        dtype=np.int64,
    )

    # Build flat config dict for Rust
    dc_tiers = []
    if dyncomp.get("enabled", False):
        for tier in dyncomp.get("tiers", []):
            dc_tiers.append({
                "max_balance": tier.get("max_balance", float("inf")),
                "comp_pct": tier.get("comp_pct", 10.0),
            })

    pct_stop_cfg = trading.get("pct_hard_stop", trading.get("hard_stop", {}))

    rust_config = {
        "initial_balance": trading.get("initial_balance", 10000.0),
        "leverage": float(trading.get("leverage", 40)),
        "maker_fee": trading.get("maker_fee", 0.0002),
        "taker_fee": trading.get("taker_fee", 0.0005),
        "margin_per_trade": trading.get("margin_per_trade", 250.0),

        "pmax_adaptive": pmax_cfg.get("adaptive", False),
        "pmax_atr_period": min(pmax_cfg.get("atr_period", 10), 20),
        "pmax_atr_multiplier": pmax_cfg.get("atr_multiplier", 3.0),
        "pmax_ma_length": pmax_cfg.get("ma_length", 10),

        "vol_lookback": pmax_cfg.get("vol_lookback", 834),
        "flip_window": pmax_cfg.get("flip_window", 423),
        "mult_base": pmax_cfg.get("mult_base", 4.0),
        "mult_scale": pmax_cfg.get("mult_scale", 3.0),
        "ma_base": pmax_cfg.get("ma_base", 18),
        "ma_scale": pmax_cfg.get("ma_scale", 4.5),
        "atr_base": pmax_cfg.get("atr_base", 20),
        "atr_scale": pmax_cfg.get("atr_scale", 1.5),
        "update_interval": pmax_cfg.get("update_interval", 31),

        "kc_length": kc_cfg.get("length", 16),
        "kc_mult": kc_cfg.get("multiplier", 1.3),
        "kc_atr_period": kc_cfg.get("atr_period", 13),

        "max_dca": float(trading.get("max_dca_steps", 1)),
        "tp_pct": trading.get("tp_close_pct", 0.05),

        # ATR hard stop (H/L based, emergency)
        "hs_enabled": hard_stop_cfg.get("enabled", False),
        "hs_mult": hard_stop_cfg.get("atr_multiplier", 5.0),

        # PCT hard stop (percentage-based, after DCA full)
        "pct_stop_enabled": pct_stop_cfg.get("enabled", False),
        "pct_stop_loss": pct_stop_cfg.get("loss_pct", 2.5),

        # Dynamic SL (close-based ATR stop)
        "dsl_enabled": dyn_sl_cfg.get("enabled", False),
        "dsl_mult": dyn_sl_cfg.get("atr_multiplier", 2.5),
        "dsl_period": dyn_sl_cfg.get("atr_period", 12),
        "dsl_tighten": dyn_sl_cfg.get("tighten_on_dca_full", 0.95),

        "dc_enabled": dyncomp.get("enabled", False),
        "dc_tiers": dc_tiers,

        "ema_enabled": ema_filter.get("enabled", False),
        "ema_period": ema_filter.get("period", 144),
        "rsi_enabled": rsi_filter.get("enabled", False),
        "rsi_period": rsi_filter.get("period", 28),
        "rsi_ob": rsi_filter.get("overbought", 65.0),
        "rsi_os": rsi_filter.get("oversold", 35.0),
        "atr_vol_enabled": atr_vol_filter.get("enabled", False),
        "atr_vol_period": atr_vol_filter.get("atr_period", 50),
        "atr_vol_percentile": atr_vol_filter.get("min_atr_percentile", 20.0),
        "dca_step_multipliers": trading.get("dca_step_multipliers", [1.0, 1.0, 1.0, 1.0]),
        "tp_step_pcts": trading.get("tp_step_pcts", []),
        "min_ms_between_dca": trading.get("min_ms_between_dca", 0),
        "min_ms_before_reversal": trading.get("min_ms_before_reversal", 0),
    }

    # Call Rust engine
    result = scalper_engine.run_fast_backtest(
        src_arr, high_arr, low_arr, close_arr, times_arr,
        rust_config, symbol,
    )

    elapsed = time.time() - t_start
    m = result["metrics"]
    logger.info(
        "Rust backtest done: %d bars, %d trades, %.3fs | Net=%+.1f%% DD=%.1f%% WR=%.1f%%",
        len(df), m["total_trades"], elapsed, m["total_pnl_pct"], m["max_drawdown_pct"], m["win_rate"],
    )

    # Update elapsed with actual wall time
    m["elapsed_seconds"] = round(elapsed, 3)

    return FastBacktestResult(
        trades=result["trades"],
        equity_curve=_downsample(result["equity_curve"], MAX_CURVE_POINTS),
        drawdown_curve=_downsample(result["drawdown_curve"], MAX_CURVE_POINTS),
        metrics=m,
        per_symbol=result["per_symbol"],
    )


def _run_python_backtest(
    df: pd.DataFrame,
    config: dict,
    symbol: str = "ETHUSDT",
) -> FastBacktestResult:
    """Original Python backtest — fallback when Rust engine not available."""
    t_start = time.time()
    n = len(df)

    # --- Config ---
    trading = config.get("trading", {})
    strategy = config.get("strategy", {})
    tf_configs = strategy.get("timeframes", [])
    tf_cfg = tf_configs[0] if tf_configs else {}

    initial_balance = trading.get("initial_balance", 10000.0)
    leverage = trading.get("leverage", 40)
    maker_fee = trading.get("maker_fee", 0.0002)
    taker_fee = trading.get("taker_fee", 0.0005)

    # PMax config
    pmax_cfg = tf_cfg.get("pmax", strategy.get("pmax", {}))
    pmax_adaptive = pmax_cfg.get("adaptive", False)
    pmax_source = pmax_cfg.get("source", "hl2")

    # KC config
    kc_cfg = tf_cfg.get("keltner", {})
    kc_length = kc_cfg.get("length", 16)
    kc_mult = kc_cfg.get("multiplier", 1.3)
    kc_atr_period = kc_cfg.get("atr_period", 13)

    # Risk config
    max_dca = trading.get("max_dca_steps", 1)
    tp_pct = trading.get("tp_close_pct", 0.05)

    hard_stop_cfg = trading.get("hard_stop", {})
    hs_enabled = hard_stop_cfg.get("enabled", False)
    hs_mult = hard_stop_cfg.get("atr_multiplier", 5.0)
    hs_period = hard_stop_cfg.get("atr_period", 11)

    dyn_sl_cfg = trading.get("dynamic_sl", {})
    dsl_enabled = dyn_sl_cfg.get("enabled", False)
    dsl_mult = dyn_sl_cfg.get("atr_multiplier", 2.5)
    dsl_period = dyn_sl_cfg.get("atr_period", 12)
    dsl_tighten = dyn_sl_cfg.get("tighten_on_dca_full", 0.95)

    # PCT hard stop (percentage-based, after DCA full)
    pct_stop_cfg = trading.get("pct_hard_stop", {})
    pct_stop_enabled = pct_stop_cfg.get("enabled", False)
    pct_stop_loss = pct_stop_cfg.get("loss_pct", 2.5)

    # Dynamic compounding
    dyncomp = strategy.get("dynamic_comp", {})
    dc_enabled = dyncomp.get("enabled", False)
    dc_tiers = dyncomp.get("tiers", [])

    # Filters
    filters = tf_cfg.get("filters", {})
    ema_filter = filters.get("ema_trend", {})
    ema_enabled = ema_filter.get("enabled", False)
    ema_period = ema_filter.get("period", 144)
    rsi_filter = filters.get("rsi", {})
    rsi_enabled = rsi_filter.get("enabled", False)
    rsi_period = rsi_filter.get("period", 28)
    rsi_ob = rsi_filter.get("overbought", 65)
    rsi_os = rsi_filter.get("oversold", 35)

    # --- Pre-compute all indicators ---
    logger.info("Fast backtest: pre-computing indicators for %d bars...", n)

    src = _get_source(df, pmax_source)
    high_s = df["high"]
    low_s = df["low"]
    close_s = df["close"]

    # PMax
    if pmax_adaptive:
        pa, ma, di = adaptive_pmax(src, high_s, low_s, close_s, pmax_cfg)
    else:
        from core.strategy.indicators import pmax as static_pmax
        pa, ma, di = static_pmax(
            src, high_s, low_s, close_s,
            atr_period=pmax_cfg.get("atr_period", 10),
            atr_multiplier=pmax_cfg.get("atr_multiplier", 3.0),
            ma_type=pmax_cfg.get("ma_type", "EMA"),
            ma_length=pmax_cfg.get("ma_length", 10),
        )

    pa_v = pa.values
    ma_v = ma.values

    # Keltner Channel
    _, ku, kl = keltner_channel(high_s, low_s, close_s, kc_length, kc_mult, kc_atr_period)
    ku_v = ku.values
    kl_v = kl.values

    # Close, High, Low arrays
    cs = close_s.values
    hi = df["high"].values
    lo = df["low"].values
    times = df["open_time"].values if "open_time" in df.columns else np.arange(n)

    # ATR arrays for stops
    sl_atr_v = atr(high_s, low_s, close_s, dsl_period).values if dsl_enabled else None
    hs_atr_v = atr(high_s, low_s, close_s, hs_period).values if hs_enabled else None

    # Filter arrays
    ema_v = ema(close_s, ema_period).values if ema_enabled else None
    rsi_v = rsi(close_s, rsi_period).values if rsi_enabled else None
    rsi_ema_v = ema(pd.Series(rsi_v), 10).values if rsi_enabled else None

    logger.info("Fast backtest: indicators done in %.1fs, running simulation...",
                time.time() - t_start)

    # --- Helper functions ---
    def get_comp(bal):
        if not dc_enabled or not dc_tiers:
            return trading.get("margin_per_trade", 250.0) / bal * 100 if bal > 0 else 10.0
        for tier in dc_tiers:
            if bal < tier.get("max_balance", float("inf")):
                return tier.get("comp_pct", 10.0)
        return dc_tiers[-1].get("comp_pct", 2.0)

    def step_margin(bal):
        if dc_enabled and dc_tiers:
            m = bal * get_comp(bal) / 100
            return max(50, m)  # no DCA split — each step gets full margin
        return trading.get("margin_per_trade", 250.0)

    def check_filter(i, side):
        if ema_enabled and ema_v is not None and not np.isnan(ema_v[i]):
            if side == "LONG" and cs[i] < ema_v[i]:
                return False
            if side == "SHORT" and cs[i] > ema_v[i]:
                return False
        if rsi_enabled and rsi_v is not None and rsi_ema_v is not None:
            r = rsi_v[i] if not np.isnan(rsi_v[i]) else 50
            e = rsi_ema_v[i] if not np.isnan(rsi_ema_v[i]) else 50
            if side == "LONG" and r > rsi_ob and r > e:
                return False
            if side == "SHORT" and r < rsi_os and r < e:
                return False
        return True

    # --- Simulation loop ---
    bal = initial_balance
    pk = initial_balance
    mdd = 0.0
    co = 0.0   # condition: 0=flat, 1=LONG, -1=SHORT
    ae = 0.0   # avg entry
    tn = 0.0   # total notional
    dca_count = 0

    trades = []
    equity_curve = []
    trade_id = 0

    _WARMUP = 200

    for i in range(_WARMUP, n):
        if np.isnan(ma_v[i]) or np.isnan(pa_v[i]):
            continue

        # --- 1. Dynamic SL check (close-based, BEFORE hard stop) ---
        if dsl_enabled and co != 0 and tn > 0 and sl_atr_v is not None and not np.isnan(sl_atr_v[i]):
            mult = dsl_mult * (dsl_tighten if dca_count >= max_dca else 1.0)
            sl_dist = mult * sl_atr_v[i]
            triggered = False
            if co > 0 and cs[i] <= ae - sl_dist:
                triggered = True
            elif co < 0 and cs[i] >= ae + sl_dist:
                triggered = True
            if triggered:
                if co > 0:
                    p = (cs[i] - ae) / ae * 100
                else:
                    p = (ae - cs[i]) / ae * 100
                pnl = tn * p / 100
                fee = tn * taker_fee
                bal += pnl - fee
                trade_id += 1
                trades.append({
                    "id": trade_id, "symbol": symbol,
                    "side": "LONG" if co > 0 else "SHORT",
                    "entry_price": ae, "exit_price": cs[i],
                    "exit_reason": "DYN_SL",
                    "pnl_usdt": round(pnl, 4), "pnl_pct": round(p, 4),
                    "fee_usdt": round(fee, 4), "leverage": leverage,
                    "entry_time": int(times[i]), "exit_time": int(times[i]),
                    "tf_label": "3m",
                })
                co = 0.0; tn = 0.0; dca_count = 0
                continue

        # --- 2. PCT Hard Stop (percentage-based, after DCA full) ---
        if pct_stop_enabled and co != 0 and tn > 0 and dca_count >= max_dca and ae > 0:
            if co > 0:
                loss_pct = (ae - cs[i]) / ae * 100
            else:
                loss_pct = (cs[i] - ae) / ae * 100
            if loss_pct >= pct_stop_loss:
                if co > 0:
                    p = (cs[i] - ae) / ae * 100
                else:
                    p = (ae - cs[i]) / ae * 100
                pnl = tn * p / 100
                fee = tn * taker_fee
                bal += pnl - fee
                trade_id += 1
                trades.append({
                    "id": trade_id, "symbol": symbol,
                    "side": "LONG" if co > 0 else "SHORT",
                    "entry_price": ae, "exit_price": cs[i],
                    "exit_reason": "PCT_STOP",
                    "pnl_usdt": round(pnl, 4), "pnl_pct": round(p, 4),
                    "fee_usdt": round(fee, 4), "leverage": leverage,
                    "entry_time": int(times[i]), "exit_time": int(times[i]),
                    "tf_label": "3m",
                })
                co = 0.0; tn = 0.0; dca_count = 0
                continue

        # --- 3. Hard stop check (H/L based) ---
        if hs_enabled and co != 0 and tn > 0 and dca_count >= max_dca and hs_atr_v is not None and not np.isnan(hs_atr_v[i]):
            hs_dist = hs_mult * hs_atr_v[i]
            triggered = False
            if co > 0 and lo[i] <= ae - hs_dist:
                triggered = True
            elif co < 0 and hi[i] >= ae + hs_dist:
                triggered = True
            if triggered:
                exit_p = (ae - hs_dist) if co > 0 else (ae + hs_dist)
                if co > 0:
                    p = (exit_p - ae) / ae * 100
                else:
                    p = (ae - exit_p) / ae * 100
                pnl = tn * p / 100
                fee = tn * taker_fee
                bal += pnl - fee
                trade_id += 1
                trades.append({
                    "id": trade_id, "symbol": symbol,
                    "side": "LONG" if co > 0 else "SHORT",
                    "entry_price": ae, "exit_price": round(exit_p, 4),
                    "exit_reason": "HARD_STOP",
                    "pnl_usdt": round(pnl, 4), "pnl_pct": round(p, 4),
                    "fee_usdt": round(fee, 4), "leverage": leverage,
                    "entry_time": int(times[i]), "exit_time": int(times[i]),
                    "tf_label": "3m",
                })
                co = 0.0; tn = 0.0; dca_count = 0
                continue

        # --- 3. PMax crossover signals ---
        if i > 0 and not np.isnan(ma_v[i-1]) and not np.isnan(pa_v[i-1]):
            pm, pp = ma_v[i-1], pa_v[i-1]
            cm, cp = ma_v[i], pa_v[i]
            buy_cross = pm <= pp and cm > cp
            sell_cross = pm >= pp and cm < cp

            if buy_cross and co <= 0:
                # Close SHORT if open
                if co < 0 and tn > 0:
                    p = (ae - cs[i]) / ae * 100
                    pnl = tn * p / 100
                    fee = tn * taker_fee
                    bal += pnl - fee
                    trade_id += 1
                    trades.append({
                        "id": trade_id, "symbol": symbol, "side": "SHORT",
                        "entry_price": ae, "exit_price": cs[i],
                        "exit_reason": "REVERSAL_CLOSE",
                        "pnl_usdt": round(pnl, 4), "pnl_pct": round(p, 4),
                        "fee_usdt": round(fee, 4), "leverage": leverage,
                        "entry_time": int(times[i]), "exit_time": int(times[i]),
                        "tf_label": "3m",
                    })
                # Open LONG
                s = step_margin(bal)
                if check_filter(i, "LONG") and bal >= s:
                    co = 1.0; ae = cs[i]; tn = s * leverage; dca_count = 0
                    bal -= tn * taker_fee  # entry fee
                else:
                    co = 0.0; tn = 0.0

            elif sell_cross and co >= 0:
                # Close LONG if open
                if co > 0 and tn > 0:
                    p = (cs[i] - ae) / ae * 100
                    pnl = tn * p / 100
                    fee = tn * taker_fee
                    bal += pnl - fee
                    trade_id += 1
                    trades.append({
                        "id": trade_id, "symbol": symbol, "side": "LONG",
                        "entry_price": ae, "exit_price": cs[i],
                        "exit_reason": "REVERSAL_CLOSE",
                        "pnl_usdt": round(pnl, 4), "pnl_pct": round(p, 4),
                        "fee_usdt": round(fee, 4), "leverage": leverage,
                        "entry_time": int(times[i]), "exit_time": int(times[i]),
                        "tf_label": "3m",
                    })
                # Open SHORT
                s = step_margin(bal)
                if check_filter(i, "SHORT") and bal >= s:
                    co = -1.0; ae = cs[i]; tn = s * leverage; dca_count = 0
                    bal -= tn * taker_fee
                else:
                    co = 0.0; tn = 0.0

        # --- 4. Keltner DCA/TP ---
        if co != 0 and tn > 0:
            u, l = ku_v[i], kl_v[i]
            if np.isnan(u) or np.isnan(l):
                pass
            else:
                s = step_margin(bal)
                ds = s * leverage

                if co > 0:  # LONG
                    if dca_count < max_dca and lo[i] <= l:
                        # DCA at KC lower
                        old = tn; tn += ds
                        ae = (ae * old + l * ds) / tn
                        dca_count += 1
                        bal -= ds * maker_fee
                    elif dca_count > 0 and hi[i] >= u:
                        # TP at KC upper
                        c2 = tn * tp_pct
                        p2 = (u - ae) / ae * 100
                        pnl = c2 * p2 / 100
                        fee = c2 * maker_fee
                        bal += pnl - fee
                        tn -= c2
                        dca_count = max(0, dca_count - 1)
                        trade_id += 1
                        trades.append({
                            "id": trade_id, "symbol": symbol, "side": "LONG",
                            "entry_price": ae, "exit_price": u,
                            "exit_reason": "TP",
                            "pnl_usdt": round(pnl, 4), "pnl_pct": round(p2, 4),
                            "fee_usdt": round(fee, 4), "leverage": leverage,
                            "entry_time": int(times[i]), "exit_time": int(times[i]),
                            "tf_label": "3m",
                        })
                        if tn < 1:
                            co = 0.0; tn = 0.0
                else:  # SHORT
                    if dca_count < max_dca and hi[i] >= u:
                        # DCA at KC upper
                        old = tn; tn += ds
                        ae = (ae * old + u * ds) / tn
                        dca_count += 1
                        bal -= ds * maker_fee
                    elif dca_count > 0 and lo[i] <= l:
                        # TP at KC lower
                        c2 = tn * tp_pct
                        p2 = (ae - l) / ae * 100
                        pnl = c2 * p2 / 100
                        fee = c2 * maker_fee
                        bal += pnl - fee
                        tn -= c2
                        dca_count = max(0, dca_count - 1)
                        trade_id += 1
                        trades.append({
                            "id": trade_id, "symbol": symbol, "side": "SHORT",
                            "entry_price": ae, "exit_price": l,
                            "exit_reason": "TP",
                            "pnl_usdt": round(pnl, 4), "pnl_pct": round(p2, 4),
                            "fee_usdt": round(fee, 4), "leverage": leverage,
                            "entry_time": int(times[i]), "exit_time": int(times[i]),
                            "tf_label": "3m",
                        })
                        if tn < 1:
                            co = 0.0; tn = 0.0

        # --- Equity tracking ---
        if bal > pk:
            pk = bal
        dd = (pk - bal) / pk * 100 if pk > 0 else 0
        mdd = max(mdd, dd)
        if bal <= 0:
            bal = 0
            break

        # Sample equity every 10 bars to keep curve manageable
        if i % 10 == 0:
            equity_curve.append({"time": int(times[i]), "equity": round(bal, 2)})

    # Final equity point
    equity_curve.append({"time": int(times[min(n-1, i)]), "equity": round(bal, 2)})

    # --- Compute metrics ---
    net = (bal - initial_balance) / initial_balance * 100
    real_trades = [t for t in trades if t["exit_reason"] != "DCA"]
    winning = [t for t in real_trades if t["pnl_usdt"] > 0]
    losing = [t for t in real_trades if t["pnl_usdt"] < 0]
    total_trades = len(real_trades)
    wr = len(winning) / total_trades * 100 if total_trades > 0 else 0
    gross_p = sum(t["pnl_usdt"] for t in winning)
    gross_l = abs(sum(t["pnl_usdt"] for t in losing))
    pf = round(gross_p / gross_l, 2) if gross_l > 0 else (999.99 if gross_p > 0 else 0)
    total_fees = sum(t["fee_usdt"] for t in trades)

    avg_win = gross_p / len(winning) if winning else 0
    avg_loss = -gross_l / len(losing) if losing else 0

    # Sharpe
    if len(real_trades) > 1:
        pnls = [t["pnl_usdt"] for t in real_trades]
        avg_pnl = sum(pnls) / len(pnls)
        std_pnl = (sum((p - avg_pnl) ** 2 for p in pnls) / len(pnls)) ** 0.5
        sharpe = round(avg_pnl / std_pnl, 4) if std_pnl > 0 else 0
    else:
        sharpe = 0

    # Drawdown curve
    dd_curve = []
    eq_peak = initial_balance
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > eq_peak:
            eq_peak = eq
        dd_pct = (eq - eq_peak) / eq_peak * 100 if eq_peak > 0 else 0
        ru_pct = (eq - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0
        dd_curve.append({
            "time": pt["time"],
            "drawdown_pct": round(dd_pct, 4),
            "runup_pct": round(ru_pct, 4),
        })

    # Max runup
    trough = initial_balance
    max_ru_pct = 0.0
    for pt in equity_curve:
        eq = pt["equity"]
        if eq < trough:
            trough = eq
        ru = (eq - trough) / trough * 100 if trough > 0 else 0
        max_ru_pct = max(max_ru_pct, ru)

    elapsed = time.time() - t_start
    logger.info(
        "Fast backtest done: %d bars, %d trades, %.1fs | Net=%+.1f%% DD=%.1f%% WR=%.1f%%",
        n, total_trades, elapsed, net, mdd, wr,
    )

    metrics = {
        "initial_balance": initial_balance,
        "current_balance": round(bal, 2),
        "peak_balance": round(pk, 2),
        "total_pnl": round(bal - initial_balance, 2),
        "total_pnl_pct": round(net, 2),
        "total_trades": total_trades,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "win_rate": round(wr, 2),
        "total_fees": round(total_fees, 2),
        "maker_fees": round(sum(t["fee_usdt"] for t in trades if t["exit_reason"] in ("TP", "DCA")), 2),
        "taker_fees": round(sum(t["fee_usdt"] for t in trades if t["exit_reason"] not in ("TP", "DCA")), 2),
        "leverage": leverage,
        "profit_factor": pf,
        "max_drawdown_pct": round(mdd, 2),
        "max_drawdown_usdt": round(pk - min(pt["equity"] for pt in equity_curve), 2) if equity_curve else 0,
        "max_runup_pct": round(max_ru_pct, 2),
        "max_runup_usdt": round(max(pt["equity"] for pt in equity_curve) - initial_balance, 2) if equity_curve else 0,
        "gross_profit": round(gross_p, 2),
        "gross_loss": round(gross_l, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "sharpe_ratio": sharpe,
        "dynamic_comp_pct": get_comp(bal) if dc_enabled else 0,
        "elapsed_seconds": round(elapsed, 1),
        "total_bars": n,
        "dsl_count": sum(1 for t in trades if t["exit_reason"] == "DYN_SL"),
        "hs_count": sum(1 for t in trades if t["exit_reason"] == "HARD_STOP"),
        "pct_stop_count": sum(1 for t in trades if t["exit_reason"] == "PCT_STOP"),
        "rev_count": sum(1 for t in trades if t["exit_reason"] == "REVERSAL_CLOSE"),
        "tp_count": sum(1 for t in trades if t["exit_reason"] == "TP"),
        "dca_count": sum(1 for t in trades if t["exit_reason"] == "DCA"),
    }

    per_symbol = [{
        "symbol": symbol,
        "total_trades": total_trades,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "win_rate": round(wr, 2),
        "total_pnl": round(bal - initial_balance, 2),
        "total_fees": round(total_fees, 2),
        "profit_factor": pf,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
    }]

    return FastBacktestResult(
        trades=trades,
        equity_curve=_downsample(equity_curve, MAX_CURVE_POINTS),
        drawdown_curve=_downsample(dd_curve, MAX_CURVE_POINTS),
        metrics=metrics,
        per_symbol=per_symbol,
    )


def fetch_and_cache_klines(
    symbol: str, interval: str, days: int,
    cache_dir: str = "data",
) -> pd.DataFrame:
    """Fetch klines from Binance, cache as parquet. Reuse if exists."""
    import json
    import urllib.parse
    import urllib.request

    cache_path = Path(cache_dir) / f"{symbol}_{interval}_{days}d.parquet"

    if cache_path.exists():
        logger.info("Loading cached data: %s", cache_path)
        return pd.read_parquet(cache_path)

    logger.info("Fetching %s %s %dd from Binance...", symbol, interval, days)
    base = "https://fapi.binance.com"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_candles = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": current_start,
            "limit": 1500,
        }
        try:
            url = f"{base}/fapi/v1/klines"
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(
                f"{url}?{qs}", headers={"User-Agent": "ScalperBot/0.1"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning("Fetch error: %s", str(e)[:80])
            break

        if not raw:
            break

        for k in raw:
            all_candles.append({
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": int(k[6]),
            })

        current_start = int(raw[-1][6]) + 1
        if len(raw) < 1500:
            break

    df = pd.DataFrame(all_candles)
    Path(cache_dir).mkdir(exist_ok=True)
    df.to_parquet(cache_path, index=False)
    logger.info("Saved %d candles to %s", len(df), cache_path)
    return df
