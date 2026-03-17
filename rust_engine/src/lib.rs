/// scalper_engine — PyO3 Python module for high-performance trading.
///
/// Modules:
///   indicators     — EMA, ATR, RSI, Keltner Channel
///   adaptive_pmax  — Adaptive PMax continuous
///   engine         — Stateful TradingEngine (dry-run & live & backtest)
///   backtest       — Array-scan backtest using TradingEngine

mod indicators;
mod adaptive_pmax;
pub mod engine;
mod backtest;

use numpy::PyReadonlyArray1;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use backtest::{BacktestConfig, FullBacktestResult};
use engine::DynCompTier;

/// Helper: PyReadonlyArray1 -> &[f64] (zero-copy, contiguous)
fn as_slice<'py>(arr: &'py PyReadonlyArray1<'py, f64>) -> &'py [f64] {
    arr.as_slice().expect("Array must be contiguous (use np.ascontiguousarray)")
}

/// Helper: PyReadonlyArray1<i64> -> &[i64]
fn as_slice_i64<'py>(arr: &'py PyReadonlyArray1<'py, i64>) -> &'py [i64] {
    arr.as_slice().expect("Array must be contiguous")
}

/// Convert FullBacktestResult to Python dict
fn result_to_pydict(py: Python<'_>, result: &FullBacktestResult, symbol: &str, config_dict: &Bound<'_, PyDict>) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    let m = &result.metrics;

    // --- trades ---
    let trades_list = PyList::empty(py);
    for t in &result.trades {
        let td = PyDict::new(py);
        td.set_item("id", t.id)?;
        td.set_item("symbol", &t.symbol)?;
        td.set_item("side", &t.side)?;
        td.set_item("entry_price", (t.entry_price * 10000.0).round() / 10000.0)?;
        td.set_item("exit_price", (t.exit_price * 10000.0).round() / 10000.0)?;
        td.set_item("exit_reason", &t.exit_reason)?;
        td.set_item("pnl_usdt", (t.pnl_usdt * 10000.0).round() / 10000.0)?;
        td.set_item("pnl_pct", (t.pnl_pct * 10000.0).round() / 10000.0)?;
        td.set_item("fee_usdt", (t.fee_usdt * 10000.0).round() / 10000.0)?;
        td.set_item("qty_usdt", (t.qty_usdt * 100.0).round() / 100.0)?;
        td.set_item("leverage", t.leverage)?;
        td.set_item("entry_time", t.entry_time)?;
        td.set_item("exit_time", t.exit_time)?;
        td.set_item("tf_label", &t.tf_label)?;
        trades_list.append(td)?;
    }
    dict.set_item("trades", trades_list)?;

    // --- equity_curve ---
    let eq_list = PyList::empty(py);
    for e in &result.equity_curve {
        let ed = PyDict::new(py);
        ed.set_item("time", e.time)?;
        ed.set_item("equity", (e.equity * 100.0).round() / 100.0)?;
        eq_list.append(ed)?;
    }
    dict.set_item("equity_curve", eq_list)?;

    // --- metrics ---
    let md = PyDict::new(py);
    md.set_item("initial_balance", m.initial_balance)?;
    md.set_item("current_balance", (m.current_balance * 100.0).round() / 100.0)?;
    md.set_item("peak_balance", (m.peak_balance * 100.0).round() / 100.0)?;
    md.set_item("total_pnl", (m.total_pnl * 100.0).round() / 100.0)?;
    md.set_item("total_pnl_pct", (m.total_pnl_pct * 100.0).round() / 100.0)?;
    md.set_item("total_trades", m.total_trades)?;
    md.set_item("winning_trades", m.winning_trades)?;
    md.set_item("losing_trades", m.losing_trades)?;
    md.set_item("win_rate", (m.win_rate * 100.0).round() / 100.0)?;
    md.set_item("total_fees", (m.total_fees * 100.0).round() / 100.0)?;
    md.set_item("maker_fees", (m.maker_fees * 100.0).round() / 100.0)?;
    md.set_item("taker_fees", (m.taker_fees * 100.0).round() / 100.0)?;
    md.set_item("leverage", m.leverage)?;
    md.set_item("profit_factor", (m.profit_factor * 100.0).round() / 100.0)?;
    md.set_item("max_drawdown_pct", (m.max_drawdown_pct * 100.0).round() / 100.0)?;
    md.set_item("max_drawdown_usdt", (m.max_drawdown_usdt * 100.0).round() / 100.0)?;
    md.set_item("max_runup_pct", (m.max_runup_pct * 100.0).round() / 100.0)?;
    md.set_item("max_runup_usdt", (m.max_runup_usdt * 100.0).round() / 100.0)?;
    md.set_item("gross_profit", (m.gross_profit * 100.0).round() / 100.0)?;
    md.set_item("gross_loss", (m.gross_loss * 100.0).round() / 100.0)?;
    md.set_item("avg_win", (m.avg_win * 10000.0).round() / 10000.0)?;
    md.set_item("avg_loss", (m.avg_loss * 10000.0).round() / 10000.0)?;
    md.set_item("sharpe_ratio", (m.sharpe_ratio * 10000.0).round() / 10000.0)?;
    md.set_item("elapsed_seconds", (m.elapsed_seconds * 10.0).round() / 10.0)?;
    md.set_item("total_bars", m.total_bars)?;
    md.set_item("dsl_count", m.dsl_count)?;
    md.set_item("hs_count", m.hs_count)?;
    md.set_item("pct_stop_count", m.pct_stop_count)?;
    md.set_item("rev_count", m.rev_count)?;
    md.set_item("tp_count", m.tp_count)?;
    md.set_item("dca_count", m.dca_count)?;

    // dynamic_comp_pct
    let dc_pct = if let Ok(Some(dc_enabled)) = config_dict.get_item("dc_enabled") {
        if dc_enabled.extract::<bool>().unwrap_or(false) {
            let bal = m.current_balance;
            if let Ok(Some(tiers_obj)) = config_dict.get_item("dc_tiers") {
                if let Ok(tiers_list) = tiers_obj.downcast::<PyList>() {
                    let mut pct = 10.0f64;
                    for item in tiers_list.iter() {
                        if let Ok(tier_dict) = item.downcast::<PyDict>() {
                            let max_bal = tier_dict.get_item("max_balance").ok().flatten()
                                .and_then(|v| v.extract::<f64>().ok()).unwrap_or(f64::INFINITY);
                            let comp = tier_dict.get_item("comp_pct").ok().flatten()
                                .and_then(|v| v.extract::<f64>().ok()).unwrap_or(10.0);
                            if bal < max_bal { pct = comp; break; }
                            pct = comp;
                        }
                    }
                    pct
                } else { 0.0 }
            } else { 0.0 }
        } else { 0.0 }
    } else { 0.0 };
    md.set_item("dynamic_comp_pct", dc_pct)?;

    dict.set_item("metrics", md)?;

    // --- drawdown_curve ---
    let dd_list = PyList::empty(py);
    let mut eq_peak = m.initial_balance;
    for e in &result.equity_curve {
        if e.equity > eq_peak { eq_peak = e.equity; }
        let dd_pct = if eq_peak > 0.0 { (e.equity - eq_peak) / eq_peak * 100.0 } else { 0.0 };
        let ru_pct = if m.initial_balance > 0.0 { (e.equity - m.initial_balance) / m.initial_balance * 100.0 } else { 0.0 };
        let dd = PyDict::new(py);
        dd.set_item("time", e.time)?;
        dd.set_item("drawdown_pct", (dd_pct * 10000.0).round() / 10000.0)?;
        dd.set_item("runup_pct", (ru_pct * 10000.0).round() / 10000.0)?;
        dd_list.append(dd)?;
    }
    dict.set_item("drawdown_curve", dd_list)?;

    // --- per_symbol ---
    let ps_list = PyList::empty(py);
    let ps = PyDict::new(py);
    ps.set_item("symbol", symbol)?;
    ps.set_item("total_trades", m.total_trades)?;
    ps.set_item("winning_trades", m.winning_trades)?;
    ps.set_item("losing_trades", m.losing_trades)?;
    ps.set_item("win_rate", (m.win_rate * 100.0).round() / 100.0)?;
    ps.set_item("total_pnl", (m.total_pnl * 100.0).round() / 100.0)?;
    ps.set_item("total_fees", (m.total_fees * 100.0).round() / 100.0)?;
    ps.set_item("profit_factor", (m.profit_factor * 100.0).round() / 100.0)?;
    ps.set_item("avg_win", (m.avg_win * 10000.0).round() / 10000.0)?;
    ps.set_item("avg_loss", (m.avg_loss * 10000.0).round() / 10000.0)?;
    ps_list.append(ps)?;
    dict.set_item("per_symbol", ps_list)?;

    Ok(dict.unbind())
}

/// Main entry point: run_fast_backtest from Python
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn run_fast_backtest<'py>(
    py: Python<'py>,
    src: PyReadonlyArray1<'py, f64>,
    high: PyReadonlyArray1<'py, f64>,
    low: PyReadonlyArray1<'py, f64>,
    close: PyReadonlyArray1<'py, f64>,
    times: PyReadonlyArray1<'py, i64>,
    config_dict: &Bound<'py, PyDict>,
    symbol: &str,
) -> PyResult<Py<PyDict>> {
    let config = parse_config(config_dict)?;

    let result = backtest::run_fast_backtest(
        as_slice(&src),
        as_slice(&high),
        as_slice(&low),
        as_slice(&close),
        as_slice_i64(&times),
        &config,
    );

    result_to_pydict(py, &result, symbol, config_dict)
}

/// Parse Python config dict to BacktestConfig
fn parse_config(d: &Bound<'_, PyDict>) -> PyResult<BacktestConfig> {
    let get_f = |key: &str, default: f64| -> f64 {
        d.get_item(key).ok().flatten()
            .and_then(|v| v.extract::<f64>().ok())
            .unwrap_or(default)
    };
    let get_u = |key: &str, default: usize| -> usize {
        d.get_item(key).ok().flatten()
            .and_then(|v| v.extract::<usize>().ok())
            .unwrap_or(default)
    };
    let get_b = |key: &str, default: bool| -> bool {
        d.get_item(key).ok().flatten()
            .and_then(|v| v.extract::<bool>().ok())
            .unwrap_or(default)
    };

    // Parse dynamic comp tiers
    let mut dc_tiers: Vec<DynCompTier> = Vec::new();
    if let Ok(Some(tiers_obj)) = d.get_item("dc_tiers") {
        if let Ok(tiers_list) = tiers_obj.downcast::<PyList>() {
            for item in tiers_list.iter() {
                if let Ok(tier_dict) = item.downcast::<PyDict>() {
                    let max_bal = tier_dict.get_item("max_balance").ok().flatten()
                        .and_then(|v| v.extract::<f64>().ok())
                        .unwrap_or(f64::INFINITY);
                    let pct = tier_dict.get_item("comp_pct").ok().flatten()
                        .and_then(|v| v.extract::<f64>().ok())
                        .unwrap_or(10.0);
                    dc_tiers.push(DynCompTier { max_balance: max_bal, comp_pct: pct });
                }
            }
        }
    }

    Ok(BacktestConfig {
        initial_balance: get_f("initial_balance", 10000.0),
        leverage: get_f("leverage", 40.0),
        maker_fee: get_f("maker_fee", 0.0002),
        taker_fee: get_f("taker_fee", 0.0005),
        margin_per_trade: get_f("margin_per_trade", 250.0),

        pmax_adaptive: get_b("pmax_adaptive", false),
        pmax_atr_period: get_u("pmax_atr_period", 10),
        pmax_atr_multiplier: get_f("pmax_atr_multiplier", 3.0),
        pmax_ma_length: get_u("pmax_ma_length", 10),

        vol_lookback: get_u("vol_lookback", 834),
        flip_window: get_u("flip_window", 423),
        mult_base: get_f("mult_base", 4.0),
        mult_scale: get_f("mult_scale", 3.0),
        ma_base: get_u("ma_base", 18),
        ma_scale: get_f("ma_scale", 4.5),
        atr_base: get_u("atr_base", 20),
        atr_scale: get_f("atr_scale", 1.5),
        update_interval: get_u("update_interval", 31),

        kc_length: get_u("kc_length", 16),
        kc_mult: get_f("kc_mult", 1.3),
        kc_atr_period: get_u("kc_atr_period", 13),

        max_dca: get_f("max_dca", 1.0) as i32,
        tp_pct: get_f("tp_pct", 0.05),

        hs_enabled: get_b("hs_enabled", false),
        hs_mult: get_f("hs_mult", 5.0),

        pct_stop_enabled: get_b("pct_stop_enabled", false),
        pct_stop_loss: get_f("pct_stop_loss", 2.5),

        dsl_enabled: get_b("dsl_enabled", false),
        dsl_mult: get_f("dsl_mult", 2.5),
        dsl_period: get_u("dsl_period", 12),
        dsl_tighten: get_f("dsl_tighten", 0.95),

        dc_enabled: get_b("dc_enabled", false),
        dc_tiers,

        ema_enabled: get_b("ema_enabled", false),
        ema_period: get_u("ema_period", 144),
        rsi_enabled: get_b("rsi_enabled", false),
        rsi_period: get_u("rsi_period", 28),
        rsi_ob: get_f("rsi_ob", 65.0),
        rsi_os: get_f("rsi_os", 35.0),
        atr_vol_enabled: get_b("atr_vol_enabled", false),
        atr_vol_period: get_u("atr_vol_period", 50),
        atr_vol_percentile: get_f("atr_vol_percentile", 20.0),
    })
}

#[pymodule]
fn scalper_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_fast_backtest, m)?)?;
    Ok(())
}
