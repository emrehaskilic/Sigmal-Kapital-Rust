"""V2 Walk-Forward Optimization Server.

4-stage WF pipeline served via REST API + WebSocket on port 8055:
  Step 1: Adaptive PMax (9 params, KC/DCA/TP off)
  Step 2: Keltner Channel (3 params, PMax locked)
  Step 3: Graduated DCA (4 multipliers, PMax+KC locked)
  Step 4: Graduated TP (4 ratios, PMax+KC+DCA locked)

Each step: anchored expanding window WF, Optuna TPESampler, DD-driven scoring.
After all folds, generates top-10 candidates for manual user selection.

Usage:
    python backend/optimizer_server.py
    python backend/optimizer_server.py --port 8055
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import optuna
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Project root ──
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)

import scalper_engine
from core.engine.fast_backtest import fetch_and_cache_klines
from config import load_config

optuna.logging.set_verbosity(optuna.logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("optimizer")

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

TIMEFRAME_BARS = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96}
WARMUP_BARS = 1000

STEP_DEFS = [
    {"key": "pmax", "label": "Adaptive PMax", "strategy": "wf_pmax"},
    {"key": "kc", "label": "Keltner Channel", "strategy": "wf_kc"},
    {"key": "dca", "label": "Graduated DCA", "strategy": "wf_dca"},
    {"key": "tp", "label": "Graduated TP", "strategy": "wf_tp"},
]

PMAX_PARAM_RANGES = {
    "vol_lookback": (100, 940, 40),
    "flip_window": (40, 500, 20),
    "mult_base": (1.5, 5.0, 0.25),
    "mult_scale": (0.5, 4.0, 0.25),
    "ma_base": (5, 23, 2),
    "ma_scale": (1.0, 6.0, 0.5),
    "atr_base": (5, 23, 2),
    "atr_scale": (0.5, 3.0, 0.5),
    "update_interval": (5, 60, 5),
}

KC_PARAM_RANGES = {
    "kc_length": (3, 20, 1),
    "kc_multiplier": (0.3, 2.0, 0.1),
    "kc_atr_period": (2, 15, 1),
}

# ══════════════════════════════════════════════════════════════
# Config Builders
# ══════════════════════════════════════════════════════════════

def _base_config() -> dict:
    """Common base config shared by all steps."""
    return {
        "initial_balance": 10000.0,
        "leverage": 25.0,
        "maker_fee": 0.0002,
        "taker_fee": 0.0005,
        "margin_per_trade": 300.0,
        # PMax base (fixed across all steps)
        "pmax_adaptive": True,
        "pmax_atr_period": 10,
        "pmax_atr_multiplier": 3.0,
        "pmax_ma_length": 10,
        # All stops OFF
        "pct_stop_enabled": False, "pct_stop_loss": 2.0,
        "hs_enabled": False, "hs_mult": 5.0,
        "dsl_enabled": False, "dsl_mult": 2.5, "dsl_period": 12, "dsl_tighten": 0.95,
        # All filters OFF
        "ema_enabled": False, "ema_period": 144,
        "rsi_enabled": False, "rsi_period": 28, "rsi_ob": 65.0, "rsi_os": 35.0,
        "atr_vol_enabled": False, "atr_vol_period": 50, "atr_vol_percentile": 20.0,
        # DynComp OFF
        "dc_enabled": False,
        # Cooldowns off
        "min_ms_between_dca": 0,
        "min_ms_before_reversal": 0,
    }


def build_pmax_config(pmax_params: dict) -> dict:
    """Step 1: Pure PMax — no KC, no DCA, no TP."""
    cfg = _base_config()
    cfg.update(pmax_params)
    cfg.update({
        "kc_length": 1, "kc_mult": 0.0, "kc_atr_period": 1,
        "max_dca": 0, "tp_pct": 0.0,
        "dca_step_multipliers": [1.0], "tp_step_pcts": [],
    })
    return cfg


def build_kc_config(kc_params: dict, locked_pmax: dict) -> dict:
    """Step 2: KC optimized, PMax locked, flat DCA=4 + TP=50%."""
    cfg = _base_config()
    cfg.update(locked_pmax)
    cfg.update({
        "kc_length": kc_params["kc_length"],
        "kc_mult": kc_params["kc_multiplier"],
        "kc_atr_period": kc_params["kc_atr_period"],
        "max_dca": 4, "tp_pct": 0.50,
        "dca_step_multipliers": [1.0, 1.0, 1.0, 1.0], "tp_step_pcts": [],
    })
    return cfg


def build_dca_config(dca_mults: list, locked_pmax: dict, locked_kc: dict) -> dict:
    """Step 3: DCA optimized, PMax+KC locked, flat TP=50%."""
    cfg = _base_config()
    cfg.update(locked_pmax)
    cfg.update({
        "kc_length": locked_kc["kc_length"],
        "kc_mult": locked_kc["kc_multiplier"],
        "kc_atr_period": locked_kc["kc_atr_period"],
        "max_dca": 4,
        "dca_step_multipliers": list(dca_mults),
        "tp_pct": 0.50, "tp_step_pcts": [],
    })
    return cfg


def build_tp_config(tp_pcts: list, locked_pmax: dict, locked_kc: dict, locked_dca_list: list) -> dict:
    """Step 4: TP optimized, PMax+KC+DCA locked."""
    cfg = _base_config()
    cfg.update(locked_pmax)
    cfg.update({
        "kc_length": locked_kc["kc_length"],
        "kc_mult": locked_kc["kc_multiplier"],
        "kc_atr_period": locked_kc["kc_atr_period"],
        "max_dca": 4,
        "dca_step_multipliers": list(locked_dca_list),
        "tp_pct": 0.50,
        "tp_step_pcts": list(tp_pcts),
    })
    return cfg


def _make_config(step: str, params: dict, locked_params: dict) -> dict:
    """Build rust_config for any step from params + locked state."""
    pmax = locked_params.get("pmax", {})
    kc = locked_params.get("kc", {})

    if step == "pmax":
        return build_pmax_config(params)
    elif step == "kc":
        return build_kc_config(params, pmax)
    elif step == "dca":
        mults = [params["dca_m1"], params["dca_m2"], params["dca_m3"], params["dca_m4"]]
        return build_dca_config(mults, pmax, kc)
    elif step == "tp":
        tps = [params["tp1"], params["tp2"], params["tp3"], params["tp4"]]
        dca_p = locked_params.get("dca", {})
        dca_list = [dca_p.get("dca_m1", 1.0), dca_p.get("dca_m2", 1.0),
                    dca_p.get("dca_m3", 1.0), dca_p.get("dca_m4", 1.0)]
        return build_tp_config(tps, pmax, kc, dca_list)
    else:
        raise ValueError(f"Unknown step: {step}")

# ══════════════════════════════════════════════════════════════
# Optuna Objectives
# ══════════════════════════════════════════════════════════════

def create_pmax_objective(src, high, low, close, times, symbol):
    """PMax scoring: (net/dd)*wr*0.01, penalty for extreme net."""
    def objective(trial):
        params = {}
        for name, (lo, hi, step) in PMAX_PARAM_RANGES.items():
            if isinstance(step, float):
                params[name] = trial.suggest_float(name, lo, hi, step=step)
            else:
                params[name] = trial.suggest_int(name, lo, hi, step=step)

        cfg = build_pmax_config(params)
        try:
            result = scalper_engine.run_fast_backtest(src, high, low, close, times, cfg, symbol)
        except Exception:
            return -999

        m = result["metrics"]
        if m["total_trades"] < 10:
            return -999

        net = m["total_pnl_pct"]
        dd = max(m["max_drawdown_pct"], 1.0)
        wr = m["win_rate"]

        score = (net / dd) * wr * 0.01
        if net > 500:
            score *= 0.8
        if net > 1000:
            score *= 0.5
        return score

    return objective


def create_kc_objective(src, high, low, close, times, locked_pmax, symbol):
    """KC scoring: DD-driven, WR>80 DD<20 hard filters."""
    def objective(trial):
        params = {}
        for name, (lo, hi, step) in KC_PARAM_RANGES.items():
            if isinstance(step, float):
                params[name] = trial.suggest_float(name, lo, hi, step=step)
            else:
                params[name] = trial.suggest_int(name, lo, hi, step=step)

        cfg = build_kc_config(params, locked_pmax)
        try:
            result = scalper_engine.run_fast_backtest(src, high, low, close, times, cfg, symbol)
        except Exception:
            return -999

        m = result["metrics"]
        if m["total_trades"] < 10:
            return -999

        net = m["total_pnl_pct"]
        dd = m["max_drawdown_pct"]
        wr = m["win_rate"]

        if wr < 80 or dd > 20:
            return -999
        return (net / max(dd, 0.5)) * wr

    return objective


def create_dca_objective(src, high, low, close, times, locked_pmax, locked_kc, symbol):
    """DCA scoring: constrained m1<=m2<=m3<=m4, DD-driven."""
    def objective(trial):
        m1 = trial.suggest_float("dca_m1", 0.5, 2.0, step=0.25)
        m2 = trial.suggest_float("dca_m2", m1, 3.0, step=0.25)
        m3 = trial.suggest_float("dca_m3", m2, 4.0, step=0.25)
        m4 = trial.suggest_float("dca_m4", m3, 5.0, step=0.25)

        cfg = build_dca_config([m1, m2, m3, m4], locked_pmax, locked_kc)
        try:
            result = scalper_engine.run_fast_backtest(src, high, low, close, times, cfg, symbol)
        except Exception:
            return -999

        m = result["metrics"]
        if m["total_trades"] < 10:
            return -999
        net = m["total_pnl_pct"]
        dd = m["max_drawdown_pct"]
        wr = m["win_rate"]
        if wr < 80 or dd > 20:
            return -999
        return (net / max(dd, 0.5)) * wr

    return objective


def create_tp_objective(src, high, low, close, times, locked_pmax, locked_kc, locked_dca_list, symbol):
    """TP scoring: constrained tp1<=tp2<=tp3<=tp4, DD-driven."""
    def objective(trial):
        tp1 = trial.suggest_float("tp1", 0.30, 0.70, step=0.05)
        tp2 = trial.suggest_float("tp2", tp1, 0.80, step=0.05)
        tp3 = trial.suggest_float("tp3", tp2, 0.90, step=0.05)
        tp4 = trial.suggest_float("tp4", tp3, 0.95, step=0.05)

        cfg = build_tp_config([tp1, tp2, tp3, tp4], locked_pmax, locked_kc, locked_dca_list)
        try:
            result = scalper_engine.run_fast_backtest(src, high, low, close, times, cfg, symbol)
        except Exception:
            return -999

        m = result["metrics"]
        if m["total_trades"] < 10:
            return -999
        net = m["total_pnl_pct"]
        dd = m["max_drawdown_pct"]
        wr = m["win_rate"]
        if wr < 80 or dd > 20:
            return -999
        return (net / max(dd, 0.5)) * wr

    return objective

# ══════════════════════════════════════════════════════════════
# Walk-Forward Engine
# ══════════════════════════════════════════════════════════════

def build_folds(total_bars: int, bars_per_day: int, min_train_days: int = 90,
                test_days: int = 7, step_days: int = 7) -> list:
    """Build anchored expanding WF folds."""
    min_train_bars = min_train_days * bars_per_day
    test_bars = test_days * bars_per_day
    step_bars = step_days * bars_per_day
    folds = []
    train_end = min_train_bars
    while train_end + test_bars <= total_bars:
        folds.append({
            "train_start": 0, "train_end": train_end,
            "test_start": train_end, "test_end": train_end + test_bars,
        })
        train_end += step_bars
    return folds


def _prepare_arrays(df, start: int, end: int):
    """Slice dataframe and return contiguous numpy arrays for Rust engine."""
    chunk = df.iloc[start:end]
    src = ((chunk["high"] + chunk["low"]) / 2).values
    return (
        np.ascontiguousarray(src, dtype=np.float64),
        np.ascontiguousarray(chunk["high"].values, dtype=np.float64),
        np.ascontiguousarray(chunk["low"].values, dtype=np.float64),
        np.ascontiguousarray(chunk["close"].values, dtype=np.float64),
        np.ascontiguousarray(chunk["open_time"].values, dtype=np.int64),
    )


def _run_test_backtest(df, fold: dict, rust_config: dict, symbol: str):
    """Backtest on fold's test period with warmup from train tail."""
    warmup_start = max(0, fold["test_start"] - WARMUP_BARS)
    test_df = df.iloc[warmup_start:fold["test_end"]].reset_index(drop=True)
    if len(test_df) < 200:
        return None

    src = ((test_df["high"] + test_df["low"]) / 2).values
    src_arr = np.ascontiguousarray(src, dtype=np.float64)
    high_arr = np.ascontiguousarray(test_df["high"].values, dtype=np.float64)
    low_arr = np.ascontiguousarray(test_df["low"].values, dtype=np.float64)
    close_arr = np.ascontiguousarray(test_df["close"].values, dtype=np.float64)
    times_arr = np.ascontiguousarray(test_df["open_time"].values, dtype=np.int64)

    try:
        result = scalper_engine.run_fast_backtest(
            src_arr, high_arr, low_arr, close_arr, times_arr, rust_config, symbol,
        )
    except Exception:
        return None
    return result["metrics"]

# ══════════════════════════════════════════════════════════════
# Consensus & Top-10
# ══════════════════════════════════════════════════════════════

def _snap(value, lo, hi, step):
    """Snap value to nearest valid step within range."""
    if isinstance(step, float):
        snapped = round(round((value - lo) / step) * step + lo, 4)
    else:
        snapped = int(round((value - lo) / step) * step + lo)
    return max(lo, min(hi, snapped))


def _compute_consensus(fold_results: list, param_names: list, param_ranges: dict):
    """Compute median params + stability from fold results."""
    vals = {n: [] for n in param_names}
    for fr in fold_results:
        if fr["best_params"] is None:
            continue
        for n in param_names:
            if n in fr["best_params"]:
                vals[n].append(fr["best_params"][n])

    consensus = {}
    stability = {}
    for n in param_names:
        v = np.array(vals[n])
        if len(v) == 0:
            continue
        lo, hi, step = param_ranges[n]
        med = _snap(float(np.median(v)), lo, hi, step)
        consensus[n] = med
        mean = float(np.mean(v))
        std = float(np.std(v))
        cv = std / abs(mean) if mean != 0 else 999
        stability[n] = {"mean": round(mean, 3), "median": med, "std": round(std, 3), "cv": round(cv, 4)}

    return consensus, stability


def compute_step_consensus(step: str, fold_results: list):
    """Dispatch consensus computation by step type."""
    if step == "pmax":
        return _compute_consensus(fold_results, list(PMAX_PARAM_RANGES.keys()), PMAX_PARAM_RANGES)
    elif step == "kc":
        return _compute_consensus(fold_results, list(KC_PARAM_RANGES.keys()), KC_PARAM_RANGES)
    elif step == "dca":
        ranges = {
            "dca_m1": (0.5, 5.0, 0.25), "dca_m2": (0.5, 5.0, 0.25),
            "dca_m3": (0.5, 5.0, 0.25), "dca_m4": (0.5, 5.0, 0.25),
        }
        return _compute_consensus(fold_results, ["dca_m1", "dca_m2", "dca_m3", "dca_m4"], ranges)
    elif step == "tp":
        ranges = {
            "tp1": (0.30, 0.95, 0.05), "tp2": (0.30, 0.95, 0.05),
            "tp3": (0.30, 0.95, 0.05), "tp4": (0.30, 0.95, 0.05),
        }
        return _compute_consensus(fold_results, ["tp1", "tp2", "tp3", "tp4"], ranges)
    return {}, {}


def _params_hash(params: dict) -> tuple:
    """Hashable representation for dedup."""
    return tuple(sorted((k, round(v, 4) if isinstance(v, float) else v) for k, v in params.items()))


def generate_top10(df, folds, fold_results, step, locked_params, symbol, bars_per_day, emit_log):
    """Evaluate consensus + best fold params across ALL test windows → top-10."""
    consensus, stability = compute_step_consensus(step, fold_results)
    if not consensus:
        emit_log("Konsensus hesaplanamadı — tüm fold'lar başarısız", "error")
        return [], {}, {}

    # Collect candidate param sets: consensus + top fold params
    candidates = [consensus]
    seen = {_params_hash(consensus)}

    valid_folds = sorted(
        [f for f in fold_results if f["best_params"]],
        key=lambda f: f.get("test_pnl_pct", 0), reverse=True,
    )
    for fr in valid_folds:
        h = _params_hash(fr["best_params"])
        if h not in seen:
            candidates.append(fr["best_params"])
            seen.add(h)
            if len(candidates) >= 15:
                break

    emit_log(f"Top-10 oluşturuluyor: {len(candidates)} aday × {len(folds)} fold...", "info")

    top10 = []
    for ci, params in enumerate(candidates):
        cfg = _make_config(step, params, locked_params)
        fold_metrics = []

        for fi, fold in enumerate(folds):
            metrics = _run_test_backtest(df, fold, cfg, symbol)
            if metrics:
                tsd = fold["test_start"] // bars_per_day
                ted = fold["test_end"] // bars_per_day
                fold_metrics.append({
                    "fold": fi + 1,
                    "test_net": round(metrics["total_pnl_pct"], 2),
                    "test_dd": round(metrics["max_drawdown_pct"], 1),
                    "test_wr": round(metrics["win_rate"], 1),
                    "test_trades": metrics["total_trades"],
                    "test_period": f"d{tsd}-d{ted}",
                })

        if not fold_metrics:
            continue

        nets = [f["test_net"] for f in fold_metrics]
        dds = [f["test_dd"] for f in fold_metrics]
        wrs = [f["test_wr"] for f in fold_metrics]

        total_net = sum(nets)
        max_dd = max(dds) if dds else 0
        avg_wr = sum(wrs) / len(wrs) if wrs else 0
        profitable_weeks = sum(1 for n in nets if n > 0)
        total_trades = sum(f["test_trades"] for f in fold_metrics)

        score = (total_net / max(max_dd, 0.5)) * avg_wr * 0.01 if max_dd > 0 else 0

        top10.append({
            "tid": ci,
            "score": round(score, 2),
            "total_net": round(total_net, 2),
            "max_dd": round(max_dd, 1),
            "avg_wr": round(avg_wr, 1),
            "profitable_weeks": profitable_weeks,
            "worst_week": round(min(nets), 2) if nets else 0,
            "best_week": round(max(nets), 2) if nets else 0,
            "total_trades": total_trades,
            "params": params,
            "folds": fold_metrics,
            # Compatibility fields for frontend
            "oos_net": round(total_net, 2),
            "dd": round(max_dd, 1),
            "wr": round(avg_wr, 1),
            "is_net": round(total_net, 2),
        })

    top10.sort(key=lambda x: x["score"], reverse=True)
    return top10[:10], consensus, stability

# ══════════════════════════════════════════════════════════════
# WF Step Runner (runs in background thread)
# ══════════════════════════════════════════════════════════════

def run_wf_step(step: str, df, folds: list, bars_per_day: int, n_trials: int,
                locked_params: dict, symbol: str,
                cancel_event: threading.Event, emit_ws, emit_log):
    """Run complete WF optimization for one step. Returns (top10, consensus, stability)."""
    total_folds = len(folds)

    emit_ws({
        "type": "walkforward_started",
        "data": {
            "total_folds": total_folds,
            "train_days": folds[0]["train_end"] // bars_per_day if folds else 0,
            "test_days": (folds[0]["test_end"] - folds[0]["test_start"]) // bars_per_day if folds else 7,
            "step_days": 7,
            "trials_per_fold": n_trials,
            "step": step,
        },
    })

    fold_results = []
    t_start = time.time()

    for fi, fold in enumerate(folds):
        if cancel_event.is_set():
            emit_log("Optimizasyon durduruldu!", "warn")
            break

        train_days = fold["train_end"] // bars_per_day
        tsd = fold["test_start"] // bars_per_day
        ted = fold["test_end"] // bars_per_day

        emit_ws({
            "type": "fold_started",
            "data": {
                "fold": fi + 1,
                "total_folds": total_folds,
                "n_trials": n_trials,
                "train_period": f"0-{train_days}d",
                "test_period": f"d{tsd}-d{ted}",
            },
        })

        fold_t0 = time.time()

        # Prepare train arrays
        src, high, low, close, times = _prepare_arrays(df, fold["train_start"], fold["train_end"])

        # Create step-specific objective
        pmax_locked = locked_params.get("pmax", {})
        kc_locked = locked_params.get("kc", {})

        if step == "pmax":
            objective = create_pmax_objective(src, high, low, close, times, symbol)
        elif step == "kc":
            objective = create_kc_objective(src, high, low, close, times, pmax_locked, symbol)
        elif step == "dca":
            objective = create_dca_objective(src, high, low, close, times, pmax_locked, kc_locked, symbol)
        elif step == "tp":
            dca_p = locked_params.get("dca", {})
            dca_list = [dca_p.get("dca_m1", 1.0), dca_p.get("dca_m2", 1.0),
                        dca_p.get("dca_m3", 1.0), dca_p.get("dca_m4", 1.0)]
            objective = create_tp_objective(src, high, low, close, times, pmax_locked, kc_locked, dca_list, symbol)
        else:
            raise ValueError(f"Unknown step: {step}")

        # Optuna study
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(multivariate=True, seed=42 + fi, warn_independent_sampling=False),
        )

        # Trial callback: throttled WS progress updates
        trial_count = [0]
        throttle = max(1, n_trials // 50)  # ~50 updates per fold

        def make_trial_callback(fold_idx, cancel_ev, throttle_n):
            def _cb(study, trial):
                if cancel_ev.is_set():
                    study.stop()
                    return
                trial_count[0] += 1
                if trial_count[0] % throttle_n == 0 or trial_count[0] == n_trials:
                    best_val = study.best_value if study.best_value is not None else -999
                    emit_ws({
                        "type": "trial_completed",
                        "data": {
                            "fold": fold_idx + 1,
                            "count": trial_count[0],
                            "score": round(best_val, 4) if best_val > -999 else 0,
                            "oos_net": 0,
                            "dd": 0,
                            "ratio": 0,
                        },
                    })
            return _cb

        trial_count[0] = 0
        study.optimize(
            objective, n_trials=n_trials, show_progress_bar=False,
            callbacks=[make_trial_callback(fi, cancel_event, throttle)],
        )

        if cancel_event.is_set():
            break

        # Extract best
        best_params = None
        best_score = -999
        fold_status = "FAIL"
        if study.best_value is not None and study.best_value > -999:
            best_params = study.best_params
            best_score = study.best_value
            fold_status = "OK"

        # Test backtest with best params
        test_metrics = None
        if best_params:
            cfg = _make_config(step, best_params, locked_params)
            test_metrics = _run_test_backtest(df, fold, cfg, symbol)

        fold_elapsed = time.time() - fold_t0

        test_pnl = test_metrics["total_pnl_pct"] if test_metrics and fold_status == "OK" else 0
        test_wr = test_metrics["win_rate"] if test_metrics and fold_status == "OK" else 0
        test_dd = test_metrics["max_drawdown_pct"] if test_metrics and fold_status == "OK" else 0
        test_trades = test_metrics["total_trades"] if test_metrics and fold_status == "OK" else 0

        fold_result = {
            "fold": fi + 1,
            "train_days": train_days,
            "test_day_range": f"d{tsd}-d{ted}",
            "train_score": round(best_score, 4),
            "test_pnl_pct": round(test_pnl, 2),
            "test_wr": round(test_wr, 1),
            "test_dd": round(test_dd, 1),
            "test_trades": test_trades,
            "best_params": best_params,
            "status": fold_status,
            "elapsed": round(fold_elapsed, 1),
        }
        fold_results.append(fold_result)

        status_str = "WIN" if test_pnl > 0 and fold_status == "OK" else ("LOSS" if fold_status == "OK" else "FAIL")

        emit_ws({
            "type": "fold_completed",
            "data": {
                "fold": fi + 1,
                "result": {
                    "fold": fi + 1,
                    "train_period": f"0-{train_days}d",
                    "test_period": f"d{tsd}-d{ted}",
                    "test_net": round(test_pnl, 2),
                    "test_dd": round(test_dd, 1),
                    "test_wr": round(test_wr, 1),
                    "test_trades": test_trades,
                    "ratio": round(test_pnl / max(test_dd, 0.1), 2) if test_dd > 0 else 0,
                    "params": best_params,
                },
            },
        })

        emit_log(
            f"Fold {fi+1}/{total_folds} | Train {train_days}d | "
            f"Test d{tsd}-d{ted} | PnL {test_pnl:+.1f}% | WR {test_wr:.1f}% | "
            f"DD {test_dd:.1f}% | Tr {test_trades} | {status_str} | {fold_elapsed:.1f}s",
            "success" if test_pnl > 0 else "warn",
        )

    total_elapsed = time.time() - t_start

    if cancel_event.is_set():
        return None, None, None

    emit_log(f"WF tamamlandı ({total_elapsed:.0f}s). Top-10 hesaplanıyor...", "info")

    # Generate top-10 candidates
    top10, consensus, stability = generate_top10(
        df, folds, fold_results, step, locked_params, symbol, bars_per_day,
        lambda msg, lvl="info": emit_log(msg, lvl),
    )

    # Aggregate summary
    valid_folds = [f for f in fold_results if f["status"] != "FAIL"]
    pnls = [f["test_pnl_pct"] for f in valid_folds]

    aggregate = {
        "total_weeks": len(folds),
        "profitable_weeks": sum(1 for p in pnls if p > 0) if pnls else 0,
        "win_rate_weeks": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1) if pnls else 0,
        "avg_weekly_net": round(float(np.mean(pnls)), 2) if pnls else 0,
        "median_weekly_net": round(float(np.median(pnls)), 2) if pnls else 0,
        "best_week_net": round(float(max(pnls)), 2) if pnls else 0,
        "worst_week_net": round(float(min(pnls)), 2) if pnls else 0,
        "total_net": round(float(sum(pnls)), 2) if pnls else 0,
        "avg_dd": round(float(np.mean([f["test_dd"] for f in valid_folds])), 1) if valid_folds else 0,
        "max_dd": round(float(max(f["test_dd"] for f in valid_folds)), 1) if valid_folds else 0,
        "avg_wr": round(float(np.mean([f["test_wr"] for f in valid_folds])), 1) if valid_folds else 0,
        "total_trades": sum(f["test_trades"] for f in valid_folds),
        "consistency_score": 0,
    }

    emit_ws({
        "type": "walkforward_completed",
        "data": {
            "summary": {
                "step": step,
                "top10": top10,
                "aggregate": aggregate,
                "consensus": consensus,
                "stability": stability,
            },
        },
    })

    emit_log(
        f"Top-10 hazır! {aggregate['profitable_weeks']}/{aggregate['total_weeks']} fold karlı, "
        f"Ort: {aggregate['avg_weekly_net']:+.1f}%",
        "success",
    )

    return top10, consensus, stability

# ══════════════════════════════════════════════════════════════
# WebSocket Manager
# ══════════════════════════════════════════════════════════════

class WSManager:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.msg_queue: queue.Queue = queue.Queue()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)
        logger.info(f"WS connected ({len(self.clients)} clients)")

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)
        logger.info(f"WS disconnected ({len(self.clients)} clients)")

    def queue_msg(self, msg: dict):
        """Thread-safe: queue message for async broadcast."""
        self.msg_queue.put(msg)

    async def flush(self):
        """Send all queued messages to connected clients."""
        while not self.msg_queue.empty():
            try:
                msg = self.msg_queue.get_nowait()
                dead = set()
                for ws in list(self.clients):
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        dead.add(ws)
                self.clients -= dead
            except queue.Empty:
                break

# ══════════════════════════════════════════════════════════════
# Pipeline V2 State
# ══════════════════════════════════════════════════════════════

class PipelineV2:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        self.active = False
        self.symbol = "ETHUSDT"
        self.timeframe = "3m"
        self.n_trials = 500
        self.days = 360
        self.min_train = 90
        self.test_days = 7
        self.step_days = 7
        self.current_step = 0
        self.step_status = "idle"  # idle | running | awaiting_selection | completed
        self.locked_params: Dict[str, dict] = {}
        self.step_results: Dict[str, list] = {}
        self.selected_indices: Dict[str, int] = {}
        self.df = None
        self.folds = None
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def get_state(self) -> dict:
        return {
            "active": self.active,
            "current_step": self.current_step,
            "step_status": self.step_status,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "n_trials": self.n_trials,
            "locked_params": self.locked_params,
            "step_results": self.step_results,
            "selected_indices": self.selected_indices,
            "steps": STEP_DEFS,
            "total_folds": len(self.folds) if self.folds else 0,
            "days": self.days,
            "min_train": self.min_train,
            "test_days": self.test_days,
            "step_days": self.step_days,
        }

    def init(self, symbol: str, timeframe: str, n_trials: int,
             days: int = 360, min_train: int = 90, test_days: int = 7, step_days: int = 7):
        self.reset()
        self.active = True
        self.symbol = symbol
        self.timeframe = timeframe
        self.n_trials = n_trials
        self.days = days
        self.min_train = min_train
        self.test_days = test_days
        self.step_days = step_days

        bars_per_day = TIMEFRAME_BARS.get(timeframe, 480)
        logger.info(f"Fetching {days} days of {symbol} {timeframe} data...")
        self.df = fetch_and_cache_klines(symbol, timeframe, days)
        logger.info(f"Loaded {len(self.df)} bars ({len(self.df) // bars_per_day} days)")

        self.folds = build_folds(len(self.df), bars_per_day, min_train, test_days, step_days)
        logger.info(f"Built {len(self.folds)} folds")

    def start_step(self, step_key: str, ws_manager: WSManager) -> dict:
        if self._thread and self._thread.is_alive():
            return {"error": "Already running"}

        self._cancel.clear()
        self.step_status = "running"
        bars_per_day = TIMEFRAME_BARS.get(self.timeframe, 480)

        def emit_ws(msg):
            ws_manager.queue_msg(msg)

        def emit_log(message, level="info"):
            ws_manager.queue_msg({"type": "log", "data": {"message": message, "level": level}})

        def _run():
            try:
                step_label = next((s["label"] for s in STEP_DEFS if s["key"] == step_key), step_key)
                emit_ws({
                    "type": "v2_step_started",
                    "data": {"step": step_key, "label": step_label},
                })

                top10, consensus, stability = run_wf_step(
                    step=step_key,
                    df=self.df,
                    folds=self.folds,
                    bars_per_day=bars_per_day,
                    n_trials=self.n_trials,
                    locked_params=self.locked_params,
                    symbol=self.symbol,
                    cancel_event=self._cancel,
                    emit_ws=emit_ws,
                    emit_log=emit_log,
                )

                if top10 is not None:
                    self.step_results[step_key] = top10
                    self.step_status = "awaiting_selection"
                    emit_ws({
                        "type": "v2_step_completed",
                        "data": {"step": step_key, "top10": top10},
                    })
                else:
                    self.step_status = "idle"
                    emit_log("Optimizasyon iptal edildi", "warn")

            except Exception as e:
                logger.exception(f"WF step failed: {e}")
                self.step_status = "idle"
                emit_ws({"type": "error", "data": {"message": f"Optimizasyon hatası: {str(e)}"}})

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return {"ok": True}

    def select_result(self, step_key: str, index: int) -> dict:
        top10 = self.step_results.get(step_key, [])
        if not top10 or index >= len(top10):
            return {"error": "Geçersiz seçim"}

        selected = top10[index]
        params = selected["params"]

        self.locked_params[step_key] = params
        self.selected_indices[step_key] = index

        step_keys = [s["key"] for s in STEP_DEFS]
        current_idx = step_keys.index(step_key)
        next_step = current_idx + 1

        if next_step >= len(STEP_DEFS):
            self.step_status = "completed"
            self.active = False
        else:
            self.current_step = next_step
            self.step_status = "idle"

        return {"ok": True, "state": self.get_state()}

    def stop(self):
        self._cancel.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.step_status = "idle"

    def update_settings(self, timeframe=None, n_trials=None):
        if timeframe and timeframe != self.timeframe:
            self.timeframe = timeframe
            if self.df is not None:
                bars_per_day = TIMEFRAME_BARS.get(timeframe, 480)
                self.df = fetch_and_cache_klines(self.symbol, timeframe, self.days)
                self.folds = build_folds(len(self.df), bars_per_day, self.min_train, self.test_days, self.step_days)
        if n_trials:
            self.n_trials = n_trials

# ══════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════

app = FastAPI(title="V2 Walk-Forward Optimizer")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pipeline = PipelineV2()
ws_manager = WSManager()


@app.on_event("startup")
async def startup():
    asyncio.create_task(_ws_broadcaster())


async def _ws_broadcaster():
    """Background task: flush queued WS messages every 100ms."""
    while True:
        await ws_manager.flush()
        await asyncio.sleep(0.1)


# ── REST Endpoints ──

class InitRequest(BaseModel):
    symbol: str = "ETHUSDT"
    timeframe: str = "3m"
    n_trials: int = 500
    days: int = 360
    min_train: int = 90
    test_days: int = 7
    step_days: int = 7


@app.post("/api/v2/init")
async def v2_init(req: InitRequest):
    try:
        pipeline.init(req.symbol, req.timeframe, req.n_trials,
                      req.days, req.min_train, req.test_days, req.step_days)
        ws_manager.queue_msg({
            "type": "log",
            "data": {
                "message": f"Pipeline başlatıldı: {req.symbol} {req.timeframe}, "
                           f"{len(pipeline.folds)} fold, {req.n_trials} trial/fold",
                "level": "success",
            },
        })
        return {"ok": True, "state": pipeline.get_state()}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/v2/state")
async def v2_state():
    return pipeline.get_state()


class StepStartRequest(BaseModel):
    step: str


@app.post("/api/v2/step/start")
async def v2_step_start(req: StepStartRequest):
    return pipeline.start_step(req.step, ws_manager)


class StepSelectRequest(BaseModel):
    step: str
    selected_index: int


@app.post("/api/v2/step/select")
async def v2_step_select(req: StepSelectRequest):
    result = pipeline.select_result(req.step, req.selected_index)
    if "ok" in result:
        ws_manager.queue_msg({
            "type": "v2_selection_made",
            "data": {
                "step": req.step,
                "selected_index": req.selected_index,
                "locked_params": pipeline.locked_params,
                "next_step": pipeline.current_step,
            },
        })
    return result


class SettingsRequest(BaseModel):
    timeframe: Optional[str] = None
    n_trials: Optional[int] = None


@app.post("/api/v2/settings")
async def v2_settings(req: SettingsRequest):
    pipeline.update_settings(req.timeframe, req.n_trials)
    return {"ok": True, "state": pipeline.get_state()}


@app.post("/api/v2/stop")
async def v2_stop():
    pipeline.stop()
    ws_manager.queue_msg({
        "type": "log",
        "data": {"message": "Optimizasyon durduruldu", "level": "warn"},
    })
    return {"ok": True}


@app.post("/api/v2/reset")
async def v2_reset():
    pipeline.reset()
    return {"ok": True}


# ── WebSocket ──

@app.websocket("/ws/pipeline")
async def ws_pipeline(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# ══════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="V2 WF Optimizer Server")
    parser.add_argument("--port", type=int, default=8055)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    logger.info(f"Starting optimizer server on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
