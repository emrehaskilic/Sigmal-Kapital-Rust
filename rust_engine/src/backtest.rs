/// Fast backtest engine — uses TradingEngine (same logic as dryrun/live).
///
/// Pre-computes indicators once, then walks bars calling:
///   engine.process_signal() — PMax crossover
///   engine.process_candle()  — KC DCA/TP + stops
///
/// This ensures backtest results are IDENTICAL to dryrun mode.

use crate::indicators::{atr_span, ema, keltner_channel, rsi};
use crate::adaptive_pmax::{adaptive_pmax_continuous, pmax_static, PmaxResult};
use crate::engine::{TradingConfig, TradingEngine, DynCompTier, TradeEvent};

/// Equity sample
pub struct EquitySample {
    pub time: i64,
    pub equity: f64,
}

/// Full backtest result
pub struct FullBacktestResult {
    pub trades: Vec<TradeEvent>,
    pub equity_curve: Vec<EquitySample>,
    pub metrics: BacktestMetrics,
}

pub struct BacktestMetrics {
    pub initial_balance: f64,
    pub current_balance: f64,
    pub peak_balance: f64,
    pub total_pnl: f64,
    pub total_pnl_pct: f64,
    pub total_trades: i32,
    pub winning_trades: i32,
    pub losing_trades: i32,
    pub win_rate: f64,
    pub total_fees: f64,
    pub maker_fees: f64,
    pub taker_fees: f64,
    pub leverage: i32,
    pub profit_factor: f64,
    pub max_drawdown_pct: f64,
    pub max_drawdown_usdt: f64,
    pub max_runup_pct: f64,
    pub max_runup_usdt: f64,
    pub gross_profit: f64,
    pub gross_loss: f64,
    pub avg_win: f64,
    pub avg_loss: f64,
    pub sharpe_ratio: f64,
    pub elapsed_seconds: f64,
    pub total_bars: i32,
    pub dsl_count: i32,
    pub hs_count: i32,
    pub pct_stop_count: i32,
    pub rev_count: i32,
    pub tp_count: i32,
    pub dca_count: i32,
}

/// Backtest configuration — Python'dan gelen tüm parametreler
pub struct BacktestConfig {
    // Trading
    pub initial_balance: f64,
    pub leverage: f64,
    pub maker_fee: f64,
    pub taker_fee: f64,
    pub margin_per_trade: f64,

    // PMax
    pub pmax_adaptive: bool,
    pub pmax_atr_period: usize,
    pub pmax_atr_multiplier: f64,
    pub pmax_ma_length: usize,
    // Adaptive PMax params
    pub vol_lookback: usize,
    pub flip_window: usize,
    pub mult_base: f64,
    pub mult_scale: f64,
    pub ma_base: usize,
    pub ma_scale: f64,
    pub atr_base: usize,
    pub atr_scale: f64,
    pub update_interval: usize,

    // Keltner
    pub kc_length: usize,
    pub kc_mult: f64,
    pub kc_atr_period: usize,

    // Risk
    pub max_dca: i32,
    pub tp_pct: f64,

    // Hard Stop (ATR-based, H/L)
    pub hs_enabled: bool,
    pub hs_mult: f64,

    // PCT Hard Stop (percentage-based, close)
    pub pct_stop_enabled: bool,
    pub pct_stop_loss: f64,

    // Dynamic SL (close-based ATR stop)
    pub dsl_enabled: bool,
    pub dsl_mult: f64,
    pub dsl_period: usize,
    pub dsl_tighten: f64,

    // Dynamic Compounding
    pub dc_enabled: bool,
    pub dc_tiers: Vec<DynCompTier>,

    // Filters
    pub ema_enabled: bool,
    pub ema_period: usize,
    pub rsi_enabled: bool,
    pub rsi_period: usize,
    pub rsi_ob: f64,
    pub rsi_os: f64,
    // ATR Volume Filter
    pub atr_vol_enabled: bool,
    pub atr_vol_period: usize,
    pub atr_vol_percentile: f64,
    // Graduated DCA
    pub dca_step_multipliers: Vec<f64>,
    // Graduated TP
    pub tp_step_pcts: Vec<f64>,
    // Cooldowns
    pub min_ms_between_dca: i64,
    pub min_ms_before_reversal: i64,
}

const WARMUP: usize = 200;

/// Check entry filters
fn check_filter(
    i: usize,
    side: i8,
    closes: &[f64],
    ema_v: Option<&[f64]>,
    rsi_v: Option<&[f64]>,
    rsi_ema_v: Option<&[f64]>,
    atr_vol_v: Option<&[f64]>,
    config: &BacktestConfig,
) -> bool {
    if config.ema_enabled {
        if let Some(ev) = ema_v {
            if !ev[i].is_nan() {
                if side == 1 && closes[i] < ev[i] { return false; }
                if side == -1 && closes[i] > ev[i] { return false; }
            }
        }
    }
    if config.rsi_enabled {
        if let (Some(rv), Some(rev)) = (rsi_v, rsi_ema_v) {
            let r = if !rv[i].is_nan() { rv[i] } else { 50.0 };
            let e = if !rev[i].is_nan() { rev[i] } else { 50.0 };
            if side == 1 && r > config.rsi_ob && r > e { return false; }
            if side == -1 && r < config.rsi_os && r < e { return false; }
        }
    }
    // ATR Volume Filter — block entry when volatility below percentile threshold
    if config.atr_vol_enabled {
        if let Some(av) = atr_vol_v {
            if i >= 200 && !av[i].is_nan() {
                let start = i - 200;
                let window = &av[start..=i];
                let mut valid: Vec<f64> = window.iter().copied().filter(|v| !v.is_nan()).collect();
                if !valid.is_empty() {
                    valid.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
                    // np.percentile linear interpolation
                    let p = config.atr_vol_percentile / 100.0 * (valid.len() as f64 - 1.0);
                    let lo = p.floor() as usize;
                    let hi = lo + 1;
                    let frac = p - lo as f64;
                    let threshold = if hi < valid.len() {
                        valid[lo] * (1.0 - frac) + valid[hi] * frac
                    } else {
                        valid[lo]
                    };
                    if av[i] < threshold {
                        return false;
                    }
                }
            }
        }
    }
    true
}

/// Build TradingConfig from BacktestConfig
fn to_trading_config(cfg: &BacktestConfig) -> TradingConfig {
    TradingConfig {
        initial_balance: cfg.initial_balance,
        leverage: cfg.leverage,
        margin_per_trade: cfg.margin_per_trade,
        maker_fee: cfg.maker_fee,
        taker_fee: cfg.taker_fee,
        max_dca_steps: cfg.max_dca,
        tp_close_pct: cfg.tp_pct,
        dyncomp_enabled: cfg.dc_enabled,
        dyncomp_tiers: cfg.dc_tiers.clone(),
        pct_stop_enabled: cfg.pct_stop_enabled,
        pct_stop_loss: cfg.pct_stop_loss,
        dyn_sl_enabled: cfg.dsl_enabled,
        dyn_sl_atr_mult: cfg.dsl_mult,
        dyn_sl_tighten: cfg.dsl_tighten,
        hard_stop_enabled: cfg.hs_enabled,
        hard_stop_atr_mult: cfg.hs_mult,
        dca_step_multipliers: cfg.dca_step_multipliers.clone(),
        tp_step_pcts: cfg.tp_step_pcts.clone(),
        min_ms_between_dca: cfg.min_ms_between_dca,
        min_ms_before_reversal: cfg.min_ms_before_reversal,
    }
}

/// Run the full fast backtest using TradingEngine (dryrun-identical logic).
pub fn run_fast_backtest(
    src: &[f64],
    high: &[f64],
    low: &[f64],
    close: &[f64],
    times: &[i64],
    config: &BacktestConfig,
) -> FullBacktestResult {
    let start_time = std::time::Instant::now();
    let n = src.len();

    // --- 1. Pre-compute indicators ---

    // PMax
    let pmax_result: PmaxResult = if config.pmax_adaptive {
        adaptive_pmax_continuous(
            src, high, low, close,
            config.pmax_atr_period, config.pmax_atr_multiplier, config.pmax_ma_length,
            config.vol_lookback, config.flip_window,
            config.mult_base, config.mult_scale,
            config.ma_base, config.ma_scale,
            config.atr_base, config.atr_scale,
            config.update_interval,
        )
    } else {
        pmax_static(
            src, high, low, close,
            config.pmax_atr_period, config.pmax_atr_multiplier, config.pmax_ma_length,
        )
    };
    let pa_v = &pmax_result.pmax_line;
    let ma_v = &pmax_result.mavg;

    // Keltner Channel
    let (_, ku_v, kl_v) = keltner_channel(
        high, low, close, config.kc_length, config.kc_mult, config.kc_atr_period,
    );

    // ATR for Dynamic SL
    let sl_atr_v: Option<Vec<f64>> = if config.dsl_enabled {
        Some(atr_span(high, low, close, config.dsl_period))
    } else {
        None
    };

    // ATR for entry (used for hard stop distance at entry time)
    let entry_atr_v = atr_span(high, low, close, config.pmax_atr_period);

    // ATR for volume filter
    let atr_vol_v: Option<Vec<f64>> = if config.atr_vol_enabled {
        Some(atr_span(high, low, close, config.atr_vol_period))
    } else {
        None
    };

    // Filters
    let ema_v: Option<Vec<f64>> = if config.ema_enabled {
        Some(ema(close, config.ema_period))
    } else {
        None
    };
    let rsi_v: Option<Vec<f64>> = if config.rsi_enabled {
        Some(rsi(close, config.rsi_period))
    } else {
        None
    };
    let rsi_ema_v: Option<Vec<f64>> = if config.rsi_enabled {
        Some(ema(rsi_v.as_ref().unwrap(), 10))
    } else {
        None
    };

    // --- 2. Create TradingEngine ---
    let trading_config = to_trading_config(config);
    let mut engine = TradingEngine::new(trading_config);

    // --- 3. Simulation loop ---
    let mut equity_curve: Vec<EquitySample> = Vec::with_capacity(n / 10 + 2);
    let mut pk = config.initial_balance;
    let mut mdd: f64 = 0.0;

    let symbol = "ETHUSDT";
    let tf_label = "3m";

    for i in WARMUP..n {
        if ma_v[i].is_nan() || pa_v[i].is_nan() {
            continue;
        }

        // --- Process candle (stops + DCA/TP) ---
        // Only if engine has a position
        if engine.has_position(symbol, tf_label) {
            let dyn_sl_atr = if config.dsl_enabled {
                sl_atr_v.as_ref().map_or(0.0, |v| if v[i].is_nan() { 0.0 } else { v[i] })
            } else {
                0.0
            };

            let kc_u = if ku_v[i].is_nan() { f64::NAN } else { ku_v[i] };
            let kc_l = if kl_v[i].is_nan() { f64::NAN } else { kl_v[i] };

            engine.process_candle(
                symbol, tf_label,
                high[i], low[i], close[i], times[i],
                kc_u, kc_l, dyn_sl_atr,
            );
        }

        // --- PMax crossover signals ---
        if i > 0 && !ma_v[i - 1].is_nan() && !pa_v[i - 1].is_nan() {
            let pm = ma_v[i - 1];
            let pp = pa_v[i - 1];
            let cm = ma_v[i];
            let cp = pa_v[i];
            let buy_cross = pm <= pp && cm > cp;
            let sell_cross = pm >= pp && cm < cp;

            if buy_cross {
                let pass = check_filter(
                    i, 1, close,
                    ema_v.as_deref(), rsi_v.as_deref(), rsi_ema_v.as_deref(),
                    atr_vol_v.as_deref(), config,
                );
                if pass {
                    let atr_val = if entry_atr_v[i].is_nan() { 0.0 } else { entry_atr_v[i] };
                    engine.process_signal(symbol, 1, close[i], atr_val, times[i], tf_label, 1.0);
                }
            } else if sell_cross {
                let pass = check_filter(
                    i, -1, close,
                    ema_v.as_deref(), rsi_v.as_deref(), rsi_ema_v.as_deref(),
                    atr_vol_v.as_deref(), config,
                );
                if pass {
                    let atr_val = if entry_atr_v[i].is_nan() { 0.0 } else { entry_atr_v[i] };
                    engine.process_signal(symbol, -1, close[i], atr_val, times[i], tf_label, 1.0);
                }
            }
        }

        // --- Equity tracking ---
        let bal = engine.get_wallet().balance;
        if bal > pk { pk = bal; }
        let dd = if pk > 0.0 { (pk - bal) / pk * 100.0 } else { 0.0 };
        if dd > mdd { mdd = dd; }

        if bal <= 0.0 {
            equity_curve.push(EquitySample { time: times[i], equity: 0.0 });
            break;
        }

        if i % 10 == 0 {
            equity_curve.push(EquitySample { time: times[i], equity: bal });
        }
    }

    // Final equity point
    let last_idx = n.saturating_sub(1);
    let final_bal = engine.get_wallet().balance;
    equity_curve.push(EquitySample {
        time: if n > 0 { times[last_idx] } else { 0 },
        equity: final_bal,
    });

    // --- 4. Compute metrics from engine ---
    let wallet = engine.get_wallet();
    let all_trades = engine.get_trades();

    // Separate real trades from DCA events
    let real_trades: Vec<&TradeEvent> = all_trades.iter()
        .filter(|t| t.exit_reason != "DCA")
        .collect();

    let mut winning = 0i32;
    let mut losing = 0i32;
    let mut gross_p: f64 = 0.0;
    let mut gross_l: f64 = 0.0;
    let mut dsl_count = 0i32;
    let mut hs_count = 0i32;
    let mut pct_stop_count = 0i32;
    let mut rev_count = 0i32;
    let mut tp_count = 0i32;
    let mut dca_count = 0i32;

    for t in all_trades {
        match t.exit_reason.as_str() {
            "DCA" => dca_count += 1,
            "DYN_SL" => dsl_count += 1,
            "HARD_STOP" => hs_count += 1,
            "PCT_STOP" => pct_stop_count += 1,
            "REVERSAL_CLOSE" => rev_count += 1,
            "TP" => tp_count += 1,
            _ => {}
        }
    }

    for t in &real_trades {
        if t.pnl_usdt > 0.0 {
            winning += 1;
            gross_p += t.pnl_usdt;
        } else {
            losing += 1;
            gross_l += t.pnl_usdt.abs();
        }
    }

    let total_trades = real_trades.len() as i32;
    let wr = if total_trades > 0 { winning as f64 / total_trades as f64 * 100.0 } else { 0.0 };
    let pf = if gross_l > 0.0 { gross_p / gross_l } else if gross_p > 0.0 { 999.99 } else { 0.0 };
    let avg_win = if winning > 0 { gross_p / winning as f64 } else { 0.0 };
    let avg_loss = if losing > 0 { -(gross_l / losing as f64) } else { 0.0 };

    // Sharpe
    let sharpe = if real_trades.len() > 1 {
        let pnls: Vec<f64> = real_trades.iter().map(|t| t.pnl_usdt).collect();
        let avg_pnl = pnls.iter().sum::<f64>() / pnls.len() as f64;
        let variance = pnls.iter().map(|p| (p - avg_pnl).powi(2)).sum::<f64>() / pnls.len() as f64;
        let std_pnl = variance.sqrt();
        if std_pnl > 0.0 { avg_pnl / std_pnl } else { 0.0 }
    } else {
        0.0
    };

    // DD/Runup from equity curve
    let min_eq = equity_curve.iter().map(|e| e.equity).fold(f64::INFINITY, f64::min);
    let max_eq = equity_curve.iter().map(|e| e.equity).fold(f64::NEG_INFINITY, f64::max);
    let max_dd_usdt = pk - min_eq;

    let mut trough = config.initial_balance;
    let mut max_ru_pct: f64 = 0.0;
    for pt in &equity_curve {
        if pt.equity < trough { trough = pt.equity; }
        let ru = if trough > 0.0 { (pt.equity - trough) / trough * 100.0 } else { 0.0 };
        if ru > max_ru_pct { max_ru_pct = ru; }
    }

    let elapsed = start_time.elapsed().as_secs_f64();
    let net = (final_bal - config.initial_balance) / config.initial_balance * 100.0;

    let metrics = BacktestMetrics {
        initial_balance: config.initial_balance,
        current_balance: final_bal,
        peak_balance: pk,
        total_pnl: final_bal - config.initial_balance,
        total_pnl_pct: net,
        total_trades,
        winning_trades: winning,
        losing_trades: losing,
        win_rate: wr,
        total_fees: wallet.total_fees,
        maker_fees: wallet.maker_fees,
        taker_fees: wallet.taker_fees,
        leverage: config.leverage as i32,
        profit_factor: pf,
        max_drawdown_pct: mdd,
        max_drawdown_usdt: max_dd_usdt,
        max_runup_pct: max_ru_pct,
        max_runup_usdt: max_eq - config.initial_balance,
        gross_profit: gross_p,
        gross_loss: gross_l,
        avg_win,
        avg_loss,
        sharpe_ratio: sharpe,
        elapsed_seconds: elapsed,
        total_bars: n as i32,
        dsl_count,
        hs_count,
        pct_stop_count,
        rev_count,
        tp_count,
        dca_count,
    };

    FullBacktestResult {
        trades: all_trades.to_vec(),
        equity_curve,
        metrics,
    }
}
