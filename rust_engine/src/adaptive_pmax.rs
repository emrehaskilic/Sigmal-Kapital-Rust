/// Adaptive PMax Continuous — bar-by-bar parametre ayarlama.
/// Python indicators.py::adaptive_pmax() birebir port.
/// + Static PMax desteği.

use crate::indicators::{atr_rma, ema};

/// PMax sonucu (hem static hem adaptive icin)
pub struct PmaxResult {
    pub pmax_line: Vec<f64>,
    pub mavg: Vec<f64>,
    pub direction: Vec<f64>,
}

/// EMA cache: 5-24 arasi tüm EMA'lari pre-compute et
fn build_ema_cache(src: &[f64]) -> Vec<Vec<f64>> {
    let mut cache = Vec::with_capacity(25);
    for period in 0..25 {
        if period >= 5 {
            cache.push(ema(src, period));
        } else {
            cache.push(vec![]);
        }
    }
    cache
}

/// ATR_RMA cache: 5-24 arasi tüm ATR'leri pre-compute et
fn build_atr_cache(high: &[f64], low: &[f64], close: &[f64]) -> Vec<Vec<f64>> {
    let mut cache = Vec::with_capacity(25);
    for period in 0..25 {
        if period >= 5 {
            cache.push(atr_rma(high, low, close, period));
        } else {
            cache.push(vec![]);
        }
    }
    cache
}

/// Median hesapla (NaN'leri atla)
fn median_non_nan(data: &[f64]) -> f64 {
    let mut valid: Vec<f64> = data.iter().copied().filter(|v| !v.is_nan()).collect();
    if valid.is_empty() {
        return f64::NAN;
    }
    valid.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mid = valid.len() / 2;
    if valid.len() % 2 == 0 {
        (valid[mid - 1] + valid[mid]) / 2.0
    } else {
        valid[mid]
    }
}

/// Static PMax — Python pmax() birebir port.
/// change_atr=true -> atr_rma, false -> atr_sma (biz sadece rma destekliyoruz, config'de hep true)
pub fn pmax_static(
    src: &[f64],
    high: &[f64],
    low: &[f64],
    close: &[f64],
    atr_period: usize,
    atr_multiplier: f64,
    ma_length: usize,
) -> PmaxResult {
    let n = src.len();
    let atr_vals = atr_rma(high, low, close, atr_period);
    let mavg_vals = ema(src, ma_length);

    let mut long_stop = vec![f64::NAN; n];
    let mut short_stop = vec![f64::NAN; n];
    let mut direction = vec![1.0_f64; n];
    let mut pmax_line = vec![f64::NAN; n];

    for i in 1..n {
        if mavg_vals[i].is_nan() || atr_vals[i].is_nan() {
            continue;
        }

        // Long stop (support)
        let ls = mavg_vals[i] - atr_multiplier * atr_vals[i];
        let prev_ls = long_stop[i - 1];
        long_stop[i] = if prev_ls.is_nan() {
            ls
        } else if mavg_vals[i] > prev_ls {
            ls.max(prev_ls)
        } else {
            ls
        };

        // Short stop (resistance)
        let ss = mavg_vals[i] + atr_multiplier * atr_vals[i];
        let prev_ss = short_stop[i - 1];
        short_stop[i] = if prev_ss.is_nan() {
            ss
        } else if mavg_vals[i] < prev_ss {
            ss.min(prev_ss)
        } else {
            ss
        };

        // Direction
        let prev_dir = direction[i - 1];
        if prev_dir == -1.0 && !short_stop[i - 1].is_nan() && mavg_vals[i] > short_stop[i - 1] {
            direction[i] = 1.0;
        } else if prev_dir == 1.0 && !long_stop[i - 1].is_nan() && mavg_vals[i] < long_stop[i - 1] {
            direction[i] = -1.0;
        } else {
            direction[i] = prev_dir;
        }

        pmax_line[i] = if direction[i] == 1.0 {
            long_stop[i]
        } else {
            short_stop[i]
        };
    }

    PmaxResult {
        pmax_line,
        mavg: mavg_vals,
        direction,
    }
}

/// Adaptive PMax Continuous — Python'daki adaptive_pmax() birebir.
pub fn adaptive_pmax_continuous(
    src: &[f64],
    high: &[f64],
    low: &[f64],
    close: &[f64],
    base_atr_period: usize,
    base_atr_multiplier: f64,
    base_ma_length: usize,
    lookback: usize,
    flip_window: usize,
    mult_base: f64,
    mult_scale: f64,
    ma_base: usize,
    ma_scale: f64,
    atr_base: usize,
    atr_scale: f64,
    update_interval: usize,
) -> PmaxResult {
    let n = src.len();

    // Base ATR for regime detection
    let base_atr = atr_rma(high, low, close, base_atr_period);

    // Pre-compute caches
    let ma_cache = build_ema_cache(src);
    let atr_cache = build_atr_cache(high, low, close);

    // Output arrays
    let mut pmax_line = vec![f64::NAN; n];
    let mut mavg_out = vec![f64::NAN; n];
    let mut direction = vec![1.0_f64; n];
    let mut long_stop = vec![f64::NAN; n];
    let mut short_stop = vec![f64::NAN; n];

    let mut active_mult = base_atr_multiplier;
    let mut active_ma_len = base_ma_length;
    let mut active_atr_p = base_atr_period;

    for i in 1..n {
        // Update adaptive params at interval
        if i >= lookback && (update_interval <= 1 || i % update_interval == 0) {
            // 1. Vol ratio -> atr_multiplier
            let start = if i >= lookback { i - lookback } else { 0 };
            let window = &base_atr[start..=i];
            let valid_count = window.iter().filter(|v| !v.is_nan()).count();
            if valid_count > 10 {
                let median_atr = median_non_nan(window);
                let current_atr = base_atr[i];
                if !current_atr.is_nan() && median_atr > 0.0 {
                    let vol_ratio = (current_atr / median_atr).clamp(0.5, 2.0);
                    active_mult = mult_base + vol_ratio * mult_scale;
                }
            }

            // 2. Trend dist -> ma_length
            let base_ma_idx = base_ma_length.clamp(5, 24);
            let mavg_base_val = if !ma_cache[base_ma_idx].is_empty() && !ma_cache[base_ma_idx][i].is_nan() {
                ma_cache[base_ma_idx][i]
            } else {
                close[i]
            };
            let c_atr = if !base_atr[i].is_nan() { base_atr[i] } else { 1.0 };
            let trend_dist = if c_atr > 0.0 {
                ((close[i] - mavg_base_val).abs() / c_atr).min(4.0)
            } else {
                0.0
            };
            active_ma_len = (ma_base as f64 + trend_dist * ma_scale).round() as usize;
            active_ma_len = active_ma_len.clamp(5, 24);

            // 3. Flip count -> atr_period
            let flip_start = if i >= flip_window { i - flip_window } else { 0 };
            let dir_window = &direction[flip_start..i];
            let mut flips = 0usize;
            if dir_window.len() > 1 {
                for j in 1..dir_window.len() {
                    if dir_window[j] != dir_window[j - 1] {
                        flips += 1;
                    }
                }
            }
            active_atr_p = (atr_base as f64 + flips as f64 * atr_scale).round() as usize;
            active_atr_p = active_atr_p.clamp(5, 24);
        }

        // Get cached values
        let mavg_arr = &ma_cache[active_ma_len];
        let atr_arr = &atr_cache[active_atr_p];

        if mavg_arr.is_empty() || atr_arr.is_empty() {
            continue;
        }
        if mavg_arr[i].is_nan() || atr_arr[i].is_nan() {
            continue;
        }

        mavg_out[i] = mavg_arr[i];

        // Long stop
        let ls = mavg_arr[i] - active_mult * atr_arr[i];
        let prev_ls = long_stop[i - 1];
        long_stop[i] = if prev_ls.is_nan() {
            ls
        } else if mavg_arr[i] > prev_ls {
            ls.max(prev_ls)
        } else {
            ls
        };

        // Short stop
        let ss = mavg_arr[i] + active_mult * atr_arr[i];
        let prev_ss = short_stop[i - 1];
        short_stop[i] = if prev_ss.is_nan() {
            ss
        } else if mavg_arr[i] < prev_ss {
            ss.min(prev_ss)
        } else {
            ss
        };

        // Direction
        let prev_dir = direction[i - 1];
        let prev_ss_val = short_stop[i - 1];
        let prev_ls_val = long_stop[i - 1];

        if prev_dir == -1.0 && !prev_ss_val.is_nan() && mavg_arr[i] > prev_ss_val {
            direction[i] = 1.0;
        } else if prev_dir == 1.0 && !prev_ls_val.is_nan() && mavg_arr[i] < prev_ls_val {
            direction[i] = -1.0;
        } else {
            direction[i] = prev_dir;
        }

        pmax_line[i] = if direction[i] == 1.0 {
            long_stop[i]
        } else {
            short_stop[i]
        };
    }

    PmaxResult {
        pmax_line,
        mavg: mavg_out,
        direction,
    }
}
