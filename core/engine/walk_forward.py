"""Walk-Forward PMax Optimization Engine.

Anchored expanding window: training always starts from day 0,
test window slides forward by 7 days each fold.

ONLY PMax parameters are optimized (9 params).
KC, DynSL, DynComp stay FIXED — they are structural decisions.

This gives the realistic weekly performance of the strategy
when re-optimized every 7 days with all available history.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import optuna
import pandas as pd

from core.strategy.indicators import (
    adaptive_pmax,
    atr,
    ema,
    keltner_channel,
    rsi,
)

logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


# Progress state for API polling
wf_progress = {
    "window": 0,
    "total_windows": 0,
    "phase": "idle",
    "window_results": [],
}


def _get_source(df, source="hl2"):
    if source == "hl2":
        return (df["high"] + df["low"]) / 2
    elif source == "hlc3":
        return (df["high"] + df["low"] + df["close"]) / 3
    elif source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    return df["close"]


# ── Fixed parameters (NOT optimized) ──
FIXED_KC = {"kc_length": 16, "kc_mult": 1.3, "kc_atr": 13}
FIXED_SL = {"sl_mult": 2.5, "sl_period": 12, "sl_tighten": 0.95}
FIXED_COMP = [(50000, 10), (100000, 10), (200000, 5), (999999999, 2)]
FIXED_MAX_DCA = 1
FIXED_TP_PCT = 0.05


def _run_backtest_pmax_only(df, pmax_params, leverage=40, initial_balance=10000.0):
    """Fast backtest with given PMax params. KC/SL/Comp are FIXED."""
    n = len(df)
    maker_fee = 0.0002
    taker_fee = 0.0005

    # PMax
    src = _get_source(df, "hl2")
    pmax_cfg = {
        **pmax_params,
        "source": "hl2", "adaptive": True,
        "ma_type": "EMA", "atr_period": 10, "ma_length": 10, "atr_multiplier": 3.0,
        "change_atr": True, "normalize_atr": False,
    }
    pa_s, ma_s, _ = adaptive_pmax(src, df["high"], df["low"], df["close"], pmax_cfg)
    pa = pa_s.values
    ma = ma_s.values

    # Fixed KC
    _, ku, kl = keltner_channel(df["high"], df["low"], df["close"],
                                 FIXED_KC["kc_length"], FIXED_KC["kc_mult"], FIXED_KC["kc_atr"])
    ku_v, kl_v = ku.values, kl.values

    # Fixed SL ATR
    sl_atr_v = atr(df["high"], df["low"], df["close"], FIXED_SL["sl_period"]).values

    # Filters
    cs = df["close"].values
    hi = df["high"].values
    lo = df["low"].values
    ema_v = ema(df["close"], 144).values
    rsi_v = rsi(df["close"], 28).values
    rsi_ema_v = ema(pd.Series(rsi_v), 10).values

    def get_comp(bal):
        for threshold, pct in FIXED_COMP:
            if bal < threshold:
                return pct
        return FIXED_COMP[-1][1]

    def step_margin(bal):
        m = bal * get_comp(bal) / 100
        return max(100, m) / (1 + FIXED_MAX_DCA)

    def check_filter(i, side):
        if not np.isnan(ema_v[i]):
            if side == "LONG" and cs[i] < ema_v[i]: return False
            if side == "SHORT" and cs[i] > ema_v[i]: return False
        r = rsi_v[i] if not np.isnan(rsi_v[i]) else 50
        e = rsi_ema_v[i] if not np.isnan(rsi_ema_v[i]) else 50
        if side == "LONG" and r > 65 and r > e: return False
        if side == "SHORT" and r < 35 and r < e: return False
        return True

    bal = initial_balance
    pk = initial_balance
    mdd = 0.0
    co = 0.0; ae = 0.0; tn = 0.0; dca_count = 0
    tc = 0; wc = 0; lc = 0; slc = 0

    equity = []

    for i in range(200, n):
        if np.isnan(ma[i]) or np.isnan(pa[i]): continue

        # DynSL (fixed params)
        if co != 0 and tn > 0 and not np.isnan(sl_atr_v[i]):
            mult = FIXED_SL["sl_mult"] * (FIXED_SL["sl_tighten"] if dca_count >= FIXED_MAX_DCA else 1.0)
            sd = mult * sl_atr_v[i]
            triggered = False
            if co > 0 and cs[i] <= ae - sd: triggered = True
            elif co < 0 and cs[i] >= ae + sd: triggered = True
            if triggered:
                p = ((cs[i] - ae) / ae * 100) if co > 0 else ((ae - cs[i]) / ae * 100)
                bal += tn * p / 100 - tn * taker_fee
                slc += 1; tc += 1; lc += 1
                co = 0.0; tn = 0.0; dca_count = 0
                continue

        # PMax crossover
        if i > 0 and not np.isnan(ma[i-1]) and not np.isnan(pa[i-1]):
            pm, pp = ma[i-1], pa[i-1]; cm, cp = ma[i], pa[i]
            bc = pm <= pp and cm > cp
            sc = pm >= pp and cm < cp

            if bc and co <= 0:
                if co < 0 and tn > 0:
                    p = (ae - cs[i]) / ae * 100
                    bal += tn * p / 100 - tn * taker_fee; tc += 1
                    if tn * p / 100 > 0: wc += 1
                    else: lc += 1
                s = step_margin(bal)
                if check_filter(i, "LONG") and bal >= s:
                    co = 1.0; ae = cs[i]; tn = s * leverage; dca_count = 0
                    bal -= tn * taker_fee
                else: co = 0.0; tn = 0.0

            elif sc and co >= 0:
                if co > 0 and tn > 0:
                    p = (cs[i] - ae) / ae * 100
                    bal += tn * p / 100 - tn * taker_fee; tc += 1
                    if tn * p / 100 > 0: wc += 1
                    else: lc += 1
                s = step_margin(bal)
                if check_filter(i, "SHORT") and bal >= s:
                    co = -1.0; ae = cs[i]; tn = s * leverage; dca_count = 0
                    bal -= tn * taker_fee
                else: co = 0.0; tn = 0.0

        # KC DCA/TP (fixed params)
        if co != 0 and tn > 0:
            u, l = ku_v[i], kl_v[i]
            if np.isnan(u) or np.isnan(l): continue
            s = step_margin(bal); ds = s * leverage
            if co > 0:
                if dca_count < FIXED_MAX_DCA and lo[i] <= l:
                    old = tn; tn += ds; ae = (ae * old + l * ds) / tn
                    dca_count += 1; bal -= ds * maker_fee
                elif dca_count > 0 and hi[i] >= u:
                    c2 = tn * FIXED_TP_PCT; p2 = (u - ae) / ae * 100
                    pn = c2 * p2 / 100
                    bal += pn - c2 * maker_fee; tn -= c2
                    dca_count = max(0, dca_count - 1); tc += 1
                    if pn > 0: wc += 1
                    else: lc += 1
                    if tn < 1: co = 0.0; tn = 0.0
            else:
                if dca_count < FIXED_MAX_DCA and hi[i] >= u:
                    old = tn; tn += ds; ae = (ae * old + u * ds) / tn
                    dca_count += 1; bal -= ds * maker_fee
                elif dca_count > 0 and lo[i] <= l:
                    c2 = tn * FIXED_TP_PCT; p2 = (ae - l) / ae * 100
                    pn = c2 * p2 / 100
                    bal += pn - c2 * maker_fee; tn -= c2
                    dca_count = max(0, dca_count - 1); tc += 1
                    if pn > 0: wc += 1
                    else: lc += 1
                    if tn < 1: co = 0.0; tn = 0.0

        if bal > pk: pk = bal
        dd = (pk - bal) / pk * 100 if pk > 0 else 0
        mdd = max(mdd, dd)

        if i % 50 == 0:
            equity.append(round(bal, 2))

        if bal <= 0: bal = 0; break

    net = (bal - initial_balance) / initial_balance * 100
    wr = wc / tc * 100 if tc > 0 else 0
    return {
        "net_pct": net, "win_rate": wr, "max_dd": mdd,
        "balance": bal, "trades": tc, "sl_count": slc,
        "wins": wc, "losses": lc, "equity": equity,
    }


def _create_pmax_objective(df_is, leverage):
    """Optuna objective — ONLY PMax params, KC/SL fixed."""
    def objective(trial):
        pmax_params = {
            "vol_lookback": trial.suggest_int("vol_lookback", 100, 960, step=20),
            "flip_window": trial.suggest_int("flip_window", 40, 500, step=10),
            "mult_base": trial.suggest_float("mult_base", 1.5, 5.0, step=0.25),
            "mult_scale": trial.suggest_float("mult_scale", 0.5, 4.0, step=0.25),
            "ma_base": trial.suggest_int("ma_base", 8, 24),
            "ma_scale": trial.suggest_float("ma_scale", 1.0, 6.0, step=0.5),
            "atr_base": trial.suggest_int("atr_base", 8, 24),
            "atr_scale": trial.suggest_float("atr_scale", 0.5, 3.0, step=0.5),
            "update_interval": trial.suggest_int("update_interval", 10, 60),
        }

        try:
            r = _run_backtest_pmax_only(df_is, pmax_params, leverage)
        except Exception:
            return -999

        if r["trades"] < 5 or r["max_dd"] < 0.5:
            return -999

        # Score: (net / DD) * WR — penalizes high DD, rewards consistency
        net = r["net_pct"]
        dd = max(r["max_dd"], 1)
        wr = r["win_rate"]

        ratio = (net / dd) * wr * 0.01

        # Overfitting penalty: extreme returns are suspicious
        if net > 500:
            ratio *= 0.8  # dampen
        if net > 1000:
            ratio *= 0.5

        return ratio

    return objective


def run_walk_forward(
    df: pd.DataFrame,
    min_train_days: int = 90,
    test_days: int = 7,
    step_days: int = 7,
    trials_per_window: int = 150,
    leverage: int = 40,
    initial_balance: float = 10000.0,
    symbol: str = "ETHUSDT",
) -> dict:
    """Walk-forward with anchored expanding window.

    Training always starts from bar 0. Test window slides by step_days.
    Only PMax is re-optimized each fold. KC/SL/Comp fixed.

    Example (250 days):
      Fold 1: Train [0-90d]   Test [90-97d]
      Fold 2: Train [0-97d]   Test [97-104d]
      Fold 3: Train [0-104d]  Test [104-111d]
      ...expands each week
    """
    t_start = time.time()
    bars_per_day = 480  # 3m
    min_train_bars = min_train_days * bars_per_day
    test_bars = test_days * bars_per_day
    step_bars = step_days * bars_per_day
    n = len(df)

    # Build anchored expanding windows
    windows = []
    train_end = min_train_bars
    while train_end + test_bars <= n:
        windows.append({
            "train_start": 0,
            "train_end": train_end,
            "test_start": train_end,
            "test_end": train_end + test_bars,
        })
        train_end += step_bars

    if not windows:
        return {"error": "Not enough data", "windows": 0}

    wf_progress["total_windows"] = len(windows)
    wf_progress["window_results"] = []
    wf_progress["phase"] = "running"

    logger.info("Walk-Forward PMax-Only: %d folds, min_train=%dd, test=%dd, step=%dd, %d trials/fold",
                len(windows), min_train_days, test_days, step_days, trials_per_window)

    all_results = []
    cumulative_balance = initial_balance

    for wi, w in enumerate(windows):
        wf_progress["window"] = wi + 1
        wf_progress["phase"] = "optimizing"

        df_train = df.iloc[w["train_start"]:w["train_end"]].reset_index(drop=True)
        df_test = df.iloc[w["test_start"]:w["test_end"]].reset_index(drop=True)

        train_days = len(df_train) // bars_per_day

        logger.info("Fold %d/%d: Train=%d bars (%dd) Test=%d bars (%dd)",
                    wi + 1, len(windows), len(df_train), train_days,
                    len(df_test), test_days)

        # Optimize PMax on training data
        study = optuna.create_study(direction="maximize")
        objective = _create_pmax_objective(df_train, leverage)
        study.optimize(objective, n_trials=trials_per_window, show_progress_bar=False)

        best = study.best_trial
        bp = best.params
        best_pmax = {k: bp[k] for k in [
            "vol_lookback", "flip_window", "mult_base", "mult_scale",
            "ma_base", "ma_scale", "atr_base", "atr_scale", "update_interval",
        ]}

        # Test on unseen data
        wf_progress["phase"] = "testing"
        train_r = _run_backtest_pmax_only(df_train, best_pmax, leverage)
        test_r = _run_backtest_pmax_only(df_test, best_pmax, leverage)

        # Cumulative tracking
        test_return = test_r["net_pct"] / 100
        cumulative_balance *= (1 + test_return)

        fold_result = {
            "fold": wi + 1,
            "train_days": train_days,
            "train_bars": len(df_train),
            "test_bars": len(df_test),
            "train_net": round(train_r["net_pct"], 1),
            "train_dd": round(train_r["max_dd"], 1),
            "test_net": round(test_r["net_pct"], 1),
            "test_dd": round(test_r["max_dd"], 1),
            "test_wr": round(test_r["win_rate"], 1),
            "test_trades": test_r["trades"],
            "test_sl": test_r["sl_count"],
            "cumulative_balance": round(cumulative_balance, 2),
            "best_pmax": best_pmax,
            "opt_score": round(best.value, 2),
        }

        all_results.append(fold_result)
        wf_progress["window_results"] = all_results

        logger.info(
            "  F%d: Train(%dd)=%+.0f%% DD=%.0f%% | Test=%+.1f%% DD=%.0f%% WR=%.0f%% trades=%d SL=%d | Cum=$%,.0f",
            wi + 1, train_days, train_r["net_pct"], train_r["max_dd"],
            test_r["net_pct"], test_r["max_dd"], test_r["win_rate"],
            test_r["trades"], test_r["sl_count"], cumulative_balance,
        )

    # Aggregate
    elapsed = time.time() - t_start
    test_nets = [f["test_net"] for f in all_results]
    test_dds = [f["test_dd"] for f in all_results]
    test_wrs = [f["test_wr"] for f in all_results]
    profitable = sum(1 for x in test_nets if x > 0)
    total_return = (cumulative_balance - initial_balance) / initial_balance * 100

    summary = {
        "total_folds": len(windows),
        "profitable_folds": profitable,
        "losing_folds": len(windows) - profitable,
        "fold_win_rate": round(profitable / len(windows) * 100, 1),
        "avg_test_net": round(np.mean(test_nets), 1),
        "median_test_net": round(np.median(test_nets), 1),
        "min_test_net": round(min(test_nets), 1),
        "max_test_net": round(max(test_nets), 1),
        "std_test_net": round(np.std(test_nets), 1),
        "avg_test_dd": round(np.mean(test_dds), 1),
        "max_test_dd": round(max(test_dds), 1),
        "avg_test_wr": round(np.mean(test_wrs), 1),
        "cumulative_return": round(total_return, 1),
        "cumulative_balance": round(cumulative_balance, 2),
        "initial_balance": initial_balance,
        "leverage": leverage,
        "min_train_days": min_train_days,
        "test_days": test_days,
        "step_days": step_days,
        "trials_per_fold": trials_per_window,
        "elapsed_seconds": round(elapsed, 1),
        "symbol": symbol,
        "optimized": "PMax only (9 params)",
        "fixed": "KC(16/1.3/13), DynSL(2.5/12/0.95), DynComp(10/10/5/2), Lev=40x",
    }

    wf_progress["phase"] = "done"

    logger.info("=" * 60)
    logger.info("WALK-FORWARD PMAX-ONLY COMPLETE")
    logger.info("  Folds: %d (%d profitable, %d losing) = %.0f%% WR",
                len(windows), profitable, len(windows) - profitable,
                profitable / len(windows) * 100)
    logger.info("  Avg weekly: %+.1f%% | Median: %+.1f%% | Std: %.1f%%",
                np.mean(test_nets), np.median(test_nets), np.std(test_nets))
    logger.info("  Avg DD: %.1f%% | Max DD: %.1f%%", np.mean(test_dds), max(test_dds))
    logger.info("  Cumulative: $%,.0f -> $%,.0f (%+.1f%%)",
                initial_balance, cumulative_balance, total_return)
    logger.info("  Elapsed: %.0fs", elapsed)
    logger.info("=" * 60)

    return {"summary": summary, "folds": all_results}
