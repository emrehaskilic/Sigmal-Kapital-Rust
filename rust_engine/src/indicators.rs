/// Trading indicators — birebir Python/Pandas karsiligi.
/// EMA, ATR (RMA & span), RSI, Keltner Channel.

/// EMA: ewm(span=period, adjust=False).mean()
/// alpha = 2 / (period + 1)
pub fn ema(data: &[f64], period: usize) -> Vec<f64> {
    let n = data.len();
    let mut out = vec![f64::NAN; n];
    if n == 0 || period == 0 {
        return out;
    }
    let alpha = 2.0 / (period as f64 + 1.0);
    let mut prev = f64::NAN;
    for i in 0..n {
        if data[i].is_nan() {
            out[i] = prev;
            continue;
        }
        if prev.is_nan() {
            prev = data[i];
        } else {
            prev = alpha * data[i] + (1.0 - alpha) * prev;
        }
        out[i] = prev;
    }
    out
}

/// EWM with alpha (for RMA/Wilder): ewm(alpha=1/period, min_periods=period, adjust=False).mean()
pub fn ewm_alpha(data: &[f64], period: usize) -> Vec<f64> {
    let n = data.len();
    let mut out = vec![f64::NAN; n];
    if n == 0 || period == 0 {
        return out;
    }
    let alpha = 1.0 / period as f64;

    let mut count = 0usize;
    let mut prev = f64::NAN;
    for i in 0..n {
        if data[i].is_nan() {
            out[i] = f64::NAN;
            continue;
        }
        count += 1;
        if prev.is_nan() {
            prev = data[i];
        } else {
            prev = alpha * data[i] + (1.0 - alpha) * prev;
        }
        if count >= period {
            out[i] = prev;
        }
    }
    out
}

/// True Range hesapla
fn true_range(high: &[f64], low: &[f64], close: &[f64]) -> Vec<f64> {
    let n = high.len();
    let mut tr = vec![f64::NAN; n];
    if n == 0 {
        return tr;
    }
    tr[0] = high[0] - low[0];
    for i in 1..n {
        let hl = high[i] - low[i];
        let hc = (high[i] - close[i - 1]).abs();
        let lc = (low[i] - close[i - 1]).abs();
        tr[i] = hl.max(hc).max(lc);
    }
    tr
}

/// ATR_RMA: TR -> ewm(alpha=1/period, min_periods=period) — Wilder's method
pub fn atr_rma(high: &[f64], low: &[f64], close: &[f64], period: usize) -> Vec<f64> {
    let tr = true_range(high, low, close);
    ewm_alpha(&tr, period)
}

/// ATR: TR -> ewm(span=period, adjust=False) — standart EMA smoothing
pub fn atr_span(high: &[f64], low: &[f64], close: &[f64], period: usize) -> Vec<f64> {
    let tr = true_range(high, low, close);
    ema(&tr, period)
}

/// RSI: Relative Strength Index
/// delta -> gain/loss ayir -> ewm(alpha=1/period) -> 100 - 100/(1+rs)
pub fn rsi(close: &[f64], period: usize) -> Vec<f64> {
    let n = close.len();
    let mut out = vec![f64::NAN; n];
    if n < 2 || period == 0 {
        return out;
    }

    let mut gain = vec![f64::NAN; n];
    let mut loss = vec![f64::NAN; n];
    gain[0] = f64::NAN;
    loss[0] = f64::NAN;
    for i in 1..n {
        let delta = close[i] - close[i - 1];
        gain[i] = if delta > 0.0 { delta } else { 0.0 };
        loss[i] = if delta < 0.0 { -delta } else { 0.0 };
    }

    let avg_gain = ewm_alpha(&gain, period);
    let avg_loss = ewm_alpha(&loss, period);

    for i in 0..n {
        if avg_gain[i].is_nan() || avg_loss[i].is_nan() {
            continue;
        }
        if avg_loss[i] == 0.0 {
            out[i] = 100.0;
        } else {
            let rs = avg_gain[i] / avg_loss[i];
            out[i] = 100.0 - (100.0 / (1.0 + rs));
        }
    }
    out
}

/// Keltner Channel: middle=EMA(close, kc_length), upper/lower = middle ± mult * ATR_RMA
pub fn keltner_channel(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    kc_length: usize,
    kc_multiplier: f64,
    atr_period: usize,
) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let middle = ema(close, kc_length);
    let atr_vals = atr_rma(high, low, close, atr_period);
    let n = close.len();
    let mut upper = vec![f64::NAN; n];
    let mut lower = vec![f64::NAN; n];
    for i in 0..n {
        if !middle[i].is_nan() && !atr_vals[i].is_nan() {
            upper[i] = middle[i] + kc_multiplier * atr_vals[i];
            lower[i] = middle[i] - kc_multiplier * atr_vals[i];
        }
    }
    (middle, upper, lower)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ema_basic() {
        let data = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let result = ema(&data, 3);
        assert!(!result[0].is_nan());
        assert!(result[4] > result[0]);
    }

    #[test]
    fn test_atr_rma_basic() {
        let high = vec![10.0, 11.0, 12.0, 11.5, 13.0];
        let low = vec![9.0, 9.5, 10.0, 10.0, 11.0];
        let close = vec![9.5, 10.5, 11.0, 10.5, 12.0];
        let result = atr_rma(&high, &low, &close, 3);
        assert_eq!(result.len(), 5);
    }

    #[test]
    fn test_rsi_basic() {
        let close: Vec<f64> = (0..50).map(|i| 100.0 + (i as f64 * 0.5).sin() * 5.0).collect();
        let result = rsi(&close, 14);
        assert_eq!(result.len(), 50);
        for v in &result {
            if !v.is_nan() {
                assert!(*v >= 0.0 && *v <= 100.0);
            }
        }
    }
}
