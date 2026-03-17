/// Stateful Trading Engine — dry-run simulator & live executor backend.
///
/// Maintains position state, wallet, and trade history.
/// Called bar-by-bar from Python via PyO3 #[pyclass].
///
/// Same logic as backtest.rs but stateful (not array-scan):
///   process_signal() — PMax crossover → kill switch + new entry
///   process_candle()  — KC DCA/TP + stop checks

use std::collections::HashMap;

// ═══════════════════════════════════════════════════════════════════
// Config
// ═══════════════════════════════════════════════════════════════════

#[derive(Clone, Debug)]
pub struct DynCompTier {
    pub max_balance: f64,
    pub comp_pct: f64,
}

#[derive(Clone, Debug)]
pub struct TradingConfig {
    pub initial_balance: f64,
    pub leverage: f64,
    pub margin_per_trade: f64,
    pub maker_fee: f64,
    pub taker_fee: f64,
    pub max_dca_steps: i32,
    pub tp_close_pct: f64,
    // Dynamic compounding
    pub dyncomp_enabled: bool,
    pub dyncomp_tiers: Vec<DynCompTier>,
    // Percentage hard stop (after DCA full)
    pub pct_stop_enabled: bool,
    pub pct_stop_loss: f64,
    // Dynamic SL (close-based ATR stop)
    pub dyn_sl_enabled: bool,
    pub dyn_sl_atr_mult: f64,
    pub dyn_sl_tighten: f64,
    // ATR hard stop (emergency backup, H/L based)
    pub hard_stop_enabled: bool,
    pub hard_stop_atr_mult: f64,
}

// ═══════════════════════════════════════════════════════════════════
// Position State
// ═══════════════════════════════════════════════════════════════════

#[derive(Clone, Debug)]
pub struct PositionState {
    pub symbol: String,
    pub side: i8, // 1=LONG, -1=SHORT, 0=flat
    pub entry_time: i64,
    pub initial_entry_price: f64,
    pub avg_entry_price: f64,
    pub entry_atr: f64,
    pub margin_per_step: f64,
    pub total_notional: f64,
    pub total_fills: i32,
    pub dca_fills: i32,
    pub dca_wave_sold: i32,
    pub hard_stop_price: f64,
    pub pending_dca_price: f64,
    pub pending_tp_price: f64,
}

impl PositionState {
    fn new() -> Self {
        Self {
            symbol: String::new(),
            side: 0,
            entry_time: 0,
            initial_entry_price: 0.0,
            avg_entry_price: 0.0,
            entry_atr: 0.0,
            margin_per_step: 0.0,
            total_notional: 0.0,
            total_fills: 0,
            dca_fills: 0,
            dca_wave_sold: 0,
            hard_stop_price: 0.0,
            pending_dca_price: 0.0,
            pending_tp_price: 0.0,
        }
    }
}

// ═══════════════════════════════════════════════════════════════════
// Wallet State
// ═══════════════════════════════════════════════════════════════════

#[derive(Clone, Debug)]
pub struct WalletState {
    pub initial_balance: f64,
    pub balance: f64,
    pub peak_balance: f64,
    pub total_trades: i32,
    pub winning_trades: i32,
    pub losing_trades: i32,
    pub total_pnl: f64,
    pub total_fees: f64,
    pub maker_fees: f64,
    pub taker_fees: f64,
}

// ═══════════════════════════════════════════════════════════════════
// Trade Event
// ═══════════════════════════════════════════════════════════════════

#[derive(Clone, Debug)]
pub struct TradeEvent {
    pub id: i32,
    pub symbol: String,
    pub side: String,
    pub entry_price: f64,
    pub entry_time: i64,
    pub exit_price: f64,
    pub exit_time: i64,
    pub exit_reason: String,
    pub qty_usdt: f64,
    pub leverage: i32,
    pub pnl_usdt: f64,
    pub pnl_pct: f64,
    pub fee_usdt: f64,
    pub tf_label: String,
}

// ═══════════════════════════════════════════════════════════════════
// Engine Stats (for get_stats)
// ═══════════════════════════════════════════════════════════════════

#[derive(Clone, Debug)]
pub struct EngineStats {
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
    pub dynamic_comp_pct: f64,
    pub current_step_margin: f64,
}

// ═══════════════════════════════════════════════════════════════════
// Trading Engine
// ═══════════════════════════════════════════════════════════════════

pub struct TradingEngine {
    config: TradingConfig,
    wallet: WalletState,
    positions: HashMap<String, PositionState>,
    size_multipliers: HashMap<String, f64>,
    tf_labels: HashMap<String, String>,
    last_signal_ts: HashMap<String, i64>,
    trades: Vec<TradeEvent>,
    trade_counter: i32,
}

impl TradingEngine {
    pub fn new(config: TradingConfig) -> Self {
        let initial = config.initial_balance;
        Self {
            wallet: WalletState {
                initial_balance: initial,
                balance: initial,
                peak_balance: initial,
                total_trades: 0,
                winning_trades: 0,
                losing_trades: 0,
                total_pnl: 0.0,
                total_fees: 0.0,
                maker_fees: 0.0,
                taker_fees: 0.0,
            },
            config,
            positions: HashMap::new(),
            size_multipliers: HashMap::new(),
            tf_labels: HashMap::new(),
            last_signal_ts: HashMap::new(),
            trades: Vec::new(),
            trade_counter: 0,
        }
    }

    // ── Helpers ──

    fn pos_key(symbol: &str, tf_label: &str) -> String {
        if tf_label.is_empty() {
            symbol.to_string()
        } else {
            format!("{}:{}", symbol, tf_label)
        }
    }

    fn get_dynamic_comp_pct(&self) -> f64 {
        if !self.config.dyncomp_enabled || self.config.dyncomp_tiers.is_empty() {
            return 10.0;
        }
        for tier in &self.config.dyncomp_tiers {
            if self.wallet.balance < tier.max_balance {
                return tier.comp_pct;
            }
        }
        self.config.dyncomp_tiers.last().unwrap().comp_pct
    }

    fn get_step_margin(&self, size_mult: f64) -> f64 {
        if self.config.dyncomp_enabled && !self.config.dyncomp_tiers.is_empty() {
            let pct = self.get_dynamic_comp_pct();
            self.wallet.balance * pct / 100.0 * size_mult
        } else {
            self.config.margin_per_trade * size_mult
        }
    }

    fn side_str(side: i8) -> String {
        if side == 1 { "LONG".to_string() } else { "SHORT".to_string() }
    }

    fn update_peak(&mut self) {
        if self.wallet.balance > self.wallet.peak_balance {
            self.wallet.peak_balance = self.wallet.balance;
        }
    }

    fn record_pnl(&mut self, pnl: f64) {
        self.wallet.total_trades += 1;
        if pnl > 0.0 {
            self.wallet.winning_trades += 1;
        } else {
            self.wallet.losing_trades += 1;
        }
        self.wallet.total_pnl += pnl;
        self.update_peak();
    }

    // ── Close Position ──

    fn close_position(
        &mut self,
        key: &str,
        exit_price: f64,
        exit_time: i64,
        reason: &str,
    ) -> Vec<TradeEvent> {
        let pos = match self.positions.get(key) {
            Some(p) if p.side != 0 && p.total_notional > 0.0 => p.clone(),
            _ => return vec![],
        };

        self.trade_counter += 1;
        let tf_label = self.tf_labels.get(key).cloned().unwrap_or_default();
        let notional = pos.total_notional;

        let pnl_pct = if pos.side == 1 {
            (exit_price - pos.avg_entry_price) / pos.avg_entry_price * 100.0
        } else {
            (pos.avg_entry_price - exit_price) / pos.avg_entry_price * 100.0
        };
        let pnl_usdt = notional * pnl_pct / 100.0;
        let exit_fee = notional * self.config.taker_fee;

        self.wallet.balance += pnl_usdt - exit_fee;
        self.wallet.total_fees += exit_fee;
        self.wallet.taker_fees += exit_fee;
        self.record_pnl(pnl_usdt);

        let event = TradeEvent {
            id: self.trade_counter,
            symbol: pos.symbol.clone(),
            side: Self::side_str(pos.side),
            entry_price: pos.avg_entry_price,
            entry_time: pos.entry_time,
            exit_price,
            exit_time,
            exit_reason: reason.to_string(),
            qty_usdt: (notional * 100.0).round() / 100.0,
            leverage: self.config.leverage as i32,
            pnl_usdt: (pnl_usdt * 10000.0).round() / 10000.0,
            pnl_pct: (pnl_pct * 10000.0).round() / 10000.0,
            fee_usdt: (exit_fee * 10000.0).round() / 10000.0,
            tf_label,
        };
        self.trades.push(event.clone());

        // Mark position closed
        if let Some(p) = self.positions.get_mut(key) {
            p.side = 0;
            p.total_notional = 0.0;
        }

        vec![event]
    }

    // ══════════════════════════════════════════════════════════════
    // process_signal — PMax crossover → kill switch + new entry
    // ══════════════════════════════════════════════════════════════

    pub fn process_signal(
        &mut self,
        symbol: &str,
        side: i8,       // 1=LONG, -1=SHORT
        price: f64,
        atr: f64,
        timestamp: i64,
        tf_label: &str,
        size_mult: f64,
    ) -> Vec<TradeEvent> {
        let mut events = Vec::new();
        let key = Self::pos_key(symbol, tf_label);
        let size_mult = if size_mult > 0.0 { size_mult } else { 1.0 };

        // Prevent re-open from same signal timestamp
        let last_ts = self.last_signal_ts.get(&key).copied().unwrap_or(0);
        let has_pos = self.positions.get(&key).map_or(false, |p| p.side != 0);
        if timestamp == last_ts && !has_pos {
            return events;
        }

        // Kill switch: close existing opposite position
        if has_pos {
            let existing_side = self.positions[&key].side;
            if existing_side == side {
                return events; // same direction, no pyramiding
            }
            events.extend(self.close_position(&key, price, timestamp, "REVERSAL_CLOSE"));
        }

        // Calculate margin (dynamic compounding)
        let margin = self.get_step_margin(size_mult);
        if self.wallet.balance < margin {
            return events;
        }

        // Open new position
        let notional = margin * self.config.leverage;
        let entry_fee = notional * self.config.taker_fee;
        self.wallet.balance -= entry_fee;
        self.wallet.total_fees += entry_fee;
        self.wallet.taker_fees += entry_fee;

        // Hard stop price (ATR-based emergency)
        let hard_stop_price = if self.config.hard_stop_enabled && atr > 0.0 {
            let dist = self.config.hard_stop_atr_mult * atr;
            if side == 1 { price - dist } else { price + dist }
        } else {
            0.0
        };

        let pos = PositionState {
            symbol: symbol.to_string(),
            side,
            entry_time: timestamp,
            initial_entry_price: price,
            avg_entry_price: price,
            entry_atr: atr,
            margin_per_step: margin,
            total_notional: notional,
            total_fills: 1,
            dca_fills: 0,
            dca_wave_sold: 0,
            hard_stop_price,
            pending_dca_price: 0.0,
            pending_tp_price: 0.0,
        };

        self.positions.insert(key.clone(), pos);
        self.size_multipliers.insert(key.clone(), size_mult);
        self.tf_labels.insert(key.clone(), tf_label.to_string());
        self.last_signal_ts.insert(key, timestamp);

        events
    }

    // ══════════════════════════════════════════════════════════════
    // process_candle — KC DCA/TP + stop checks (one bar)
    // ══════════════════════════════════════════════════════════════

    pub fn process_candle(
        &mut self,
        symbol: &str,
        tf_label: &str,
        high: f64,
        low: f64,
        close: f64,
        timestamp: i64,
        kc_upper: f64,
        kc_lower: f64,
        dyn_sl_atr: f64,
    ) -> Vec<TradeEvent> {
        let key = Self::pos_key(symbol, tf_label);

        // Check position exists and is active
        let side = match self.positions.get(&key) {
            Some(p) if p.side != 0 && p.total_notional > 0.0 => p.side,
            _ => return vec![],
        };

        // ── 1. Dynamic SL Check (close-based) ──
        if self.config.dyn_sl_enabled && dyn_sl_atr > 0.0 {
            let pos = self.positions.get(&key).unwrap();
            let mut mult = self.config.dyn_sl_atr_mult;
            if pos.dca_fills >= self.config.max_dca_steps {
                mult *= self.config.dyn_sl_tighten;
            }
            let sl_dist = mult * dyn_sl_atr;
            let triggered = if side == 1 {
                close <= pos.avg_entry_price - sl_dist
            } else {
                close >= pos.avg_entry_price + sl_dist
            };
            if triggered {
                return self.close_position(&key, close, timestamp, "DYN_SL");
            }
        }

        // ── 2. PCT Hard Stop Check (after DCA full) ──
        if self.config.pct_stop_enabled {
            let pos = self.positions.get(&key).unwrap();
            if pos.dca_fills >= self.config.max_dca_steps && pos.avg_entry_price > 0.0 {
                let loss_pct = if side == 1 {
                    (pos.avg_entry_price - close) / pos.avg_entry_price * 100.0
                } else {
                    (close - pos.avg_entry_price) / pos.avg_entry_price * 100.0
                };
                if loss_pct >= self.config.pct_stop_loss {
                    return self.close_position(&key, close, timestamp, "PCT_STOP");
                }
            }
        }

        // ── 3. ATR Hard Stop Check (H/L based, emergency) ──
        if self.config.hard_stop_enabled {
            let pos = self.positions.get(&key).unwrap();
            if pos.hard_stop_price > 0.0 {
                let triggered = if side == 1 {
                    low <= pos.hard_stop_price
                } else {
                    high >= pos.hard_stop_price
                };
                if triggered {
                    let stop_price = pos.hard_stop_price;
                    return self.close_position(&key, stop_price, timestamp, "HARD_STOP");
                }
            }
        }

        // ── 4. Keltner Channel DCA / TP ──
        if kc_upper.is_nan() || kc_lower.is_nan() {
            return vec![];
        }

        // Update pending prices for dashboard
        {
            let pos = self.positions.get_mut(&key).unwrap();
            if side == 1 {
                pos.pending_dca_price = kc_lower;
                pos.pending_tp_price = kc_upper;
            } else {
                pos.pending_dca_price = kc_upper;
                pos.pending_tp_price = kc_lower;
            }
        }

        let max_dca = self.config.max_dca_steps;
        let tp_close_pct = self.config.tp_close_pct;

        // Clone needed values before mutable borrow
        let dca_fills = self.positions[&key].dca_fills;

        // ── DCA Check ──
        if side == 1 && dca_fills < max_dca && low <= kc_lower {
            return self.process_dca_fill(&key, kc_lower, timestamp);
        }
        if side == -1 && dca_fills < max_dca && high >= kc_upper {
            return self.process_dca_fill(&key, kc_upper, timestamp);
        }

        // ── TP Check (only if DCA occurred — no breakeven filter, matches Python) ──
        if side == 1 && dca_fills > 0 && high >= kc_upper && tp_close_pct > 0.0 {
            return self.process_tp_fill(&key, kc_upper, timestamp);
        }
        if side == -1 && dca_fills > 0 && low <= kc_lower && tp_close_pct > 0.0 {
            return self.process_tp_fill(&key, kc_lower, timestamp);
        }

        vec![]
    }

    // ── DCA Fill ──

    fn process_dca_fill(&mut self, key: &str, fill_price: f64, timestamp: i64) -> Vec<TradeEvent> {
        let size_mult = self.size_multipliers.get(key).copied().unwrap_or(1.0);
        let dca_margin = self.get_step_margin(size_mult);

        if self.wallet.balance < dca_margin {
            return vec![];
        }

        let step_notional = dca_margin * self.config.leverage;
        let dca_fee = step_notional * self.config.maker_fee;
        self.wallet.balance -= dca_fee;
        self.wallet.total_fees += dca_fee;
        self.wallet.maker_fees += dca_fee;

        let pos = self.positions.get_mut(key).unwrap();
        let old_total = pos.total_notional;
        let new_total = old_total + step_notional;
        pos.avg_entry_price = (pos.avg_entry_price * old_total + fill_price * step_notional) / new_total;
        pos.total_notional = new_total;
        pos.total_fills += 1;
        pos.dca_fills += 1;
        pos.dca_wave_sold = 0;
        pos.margin_per_step = dca_margin;

        // Update hard stop after DCA
        if self.config.hard_stop_enabled && pos.entry_atr > 0.0 {
            let dist = self.config.hard_stop_atr_mult * pos.entry_atr;
            pos.hard_stop_price = if pos.side == 1 {
                pos.avg_entry_price - dist
            } else {
                pos.avg_entry_price + dist
            };
        }

        self.trade_counter += 1;
        let tf_label = self.tf_labels.get(key).cloned().unwrap_or_default();
        let event = TradeEvent {
            id: self.trade_counter,
            symbol: pos.symbol.clone(),
            side: Self::side_str(pos.side),
            entry_price: fill_price,
            entry_time: timestamp,
            exit_price: fill_price,
            exit_time: timestamp,
            exit_reason: "DCA".to_string(),
            qty_usdt: (step_notional * 100.0).round() / 100.0,
            leverage: self.config.leverage as i32,
            pnl_usdt: 0.0,
            pnl_pct: 0.0,
            fee_usdt: (dca_fee * 10000.0).round() / 10000.0,
            tf_label,
        };
        self.trades.push(event.clone());
        vec![event]
    }

    // ── TP Fill ──

    fn process_tp_fill(&mut self, key: &str, fill_price: f64, timestamp: i64) -> Vec<TradeEvent> {
        let pos = self.positions.get(key).unwrap();
        let avg_before = pos.avg_entry_price;
        let side = pos.side;
        let closed_notional = pos.total_notional * self.config.tp_close_pct;

        if closed_notional <= 0.0 {
            return vec![];
        }

        let pnl_pct = if side == 1 {
            (fill_price - avg_before) / avg_before * 100.0
        } else {
            (avg_before - fill_price) / avg_before * 100.0
        };
        let pnl_usdt = closed_notional * pnl_pct / 100.0;
        let tp_fee = closed_notional * self.config.maker_fee;

        self.wallet.balance += pnl_usdt - tp_fee;
        self.wallet.total_fees += tp_fee;
        self.wallet.maker_fees += tp_fee;
        self.record_pnl(pnl_usdt);

        // Update position
        let pos = self.positions.get_mut(key).unwrap();
        pos.total_notional -= closed_notional;
        pos.dca_fills = (pos.dca_fills - 1).max(0);
        pos.dca_wave_sold += 1;
        pos.total_fills = (pos.total_fills - 1).max(1);

        if pos.dca_fills == 0 {
            pos.dca_wave_sold = 0;
        }
        if pos.total_notional < 1.0 {
            pos.side = 0;
            pos.total_notional = 0.0;
        }

        self.trade_counter += 1;
        let tf_label = self.tf_labels.get(key).cloned().unwrap_or_default();
        let event = TradeEvent {
            id: self.trade_counter,
            symbol: pos.symbol.clone(),
            side: Self::side_str(side),
            entry_price: avg_before,
            entry_time: pos.entry_time,
            exit_price: fill_price,
            exit_time: timestamp,
            exit_reason: "TP".to_string(),
            qty_usdt: (closed_notional * 100.0).round() / 100.0,
            leverage: self.config.leverage as i32,
            pnl_usdt: (pnl_usdt * 10000.0).round() / 10000.0,
            pnl_pct: (pnl_pct * 10000.0).round() / 10000.0,
            fee_usdt: (tp_fee * 10000.0).round() / 10000.0,
            tf_label,
        };
        self.trades.push(event.clone());
        vec![event]
    }

    // ══════════════════════════════════════════════════════════════
    // Query Methods
    // ══════════════════════════════════════════════════════════════

    pub fn has_position(&self, symbol: &str, tf_label: &str) -> bool {
        let key = Self::pos_key(symbol, tf_label);
        self.positions.get(&key).map_or(false, |p| p.side != 0)
    }

    pub fn get_position(&self, symbol: &str, tf_label: &str) -> Option<&PositionState> {
        let key = Self::pos_key(symbol, tf_label);
        self.positions.get(&key).filter(|p| p.side != 0)
    }

    pub fn get_all_positions(&self) -> &HashMap<String, PositionState> {
        &self.positions
    }

    pub fn get_wallet(&self) -> &WalletState {
        &self.wallet
    }

    pub fn get_trades(&self) -> &[TradeEvent] {
        &self.trades
    }

    pub fn get_stats(&self) -> EngineStats {
        let win_rate = if self.wallet.total_trades > 0 {
            self.wallet.winning_trades as f64 / self.wallet.total_trades as f64 * 100.0
        } else {
            0.0
        };

        let comp_pct = if self.config.dyncomp_enabled {
            self.get_dynamic_comp_pct()
        } else {
            0.0
        };
        let step_margin = if self.config.dyncomp_enabled {
            self.wallet.balance * comp_pct / 100.0
        } else {
            0.0
        };

        EngineStats {
            initial_balance: self.wallet.initial_balance,
            current_balance: (self.wallet.balance * 100.0).round() / 100.0,
            peak_balance: (self.wallet.peak_balance * 100.0).round() / 100.0,
            total_pnl: (self.wallet.total_pnl * 100.0).round() / 100.0,
            total_pnl_pct: ((self.wallet.balance - self.wallet.initial_balance)
                / self.wallet.initial_balance * 100.0 * 100.0).round() / 100.0,
            total_trades: self.wallet.total_trades,
            winning_trades: self.wallet.winning_trades,
            losing_trades: self.wallet.losing_trades,
            win_rate: (win_rate * 100.0).round() / 100.0,
            total_fees: (self.wallet.total_fees * 10000.0).round() / 10000.0,
            maker_fees: (self.wallet.maker_fees * 10000.0).round() / 10000.0,
            taker_fees: (self.wallet.taker_fees * 10000.0).round() / 10000.0,
            leverage: self.config.leverage as i32,
            dynamic_comp_pct: comp_pct,
            current_step_margin: (step_margin * 100.0).round() / 100.0,
        }
    }
}

// ═══════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> TradingConfig {
        TradingConfig {
            initial_balance: 10000.0,
            leverage: 25.0,
            margin_per_trade: 300.0,
            maker_fee: 0.0002,
            taker_fee: 0.0005,
            max_dca_steps: 4,
            tp_close_pct: 0.50,
            dyncomp_enabled: true,
            dyncomp_tiers: vec![
                DynCompTier { max_balance: 30000.0, comp_pct: 3.0 },
                DynCompTier { max_balance: 150000.0, comp_pct: 5.0 },
                DynCompTier { max_balance: 999999999.0, comp_pct: 2.25 },
            ],
            pct_stop_enabled: true,
            pct_stop_loss: 2.5,
            dyn_sl_enabled: false,
            dyn_sl_atr_mult: 2.5,
            dyn_sl_tighten: 0.95,
            hard_stop_enabled: false,
            hard_stop_atr_mult: 5.0,
        }
    }

    #[test]
    fn test_open_and_close_position() {
        let mut engine = TradingEngine::new(test_config());

        // Open LONG
        let events = engine.process_signal("ETHUSDT", 1, 2000.0, 50.0, 1000, "3m", 1.0);
        assert!(events.is_empty()); // entry doesn't produce trade events
        assert!(engine.has_position("ETHUSDT", "3m"));

        // Reverse to SHORT
        let events = engine.process_signal("ETHUSDT", -1, 2050.0, 50.0, 2000, "3m", 1.0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].exit_reason, "REVERSAL_CLOSE");
        assert!(events[0].pnl_usdt > 0.0); // 2000 → 2050, LONG = profit
    }

    #[test]
    fn test_dca_and_tp() {
        let mut engine = TradingEngine::new(test_config());
        engine.process_signal("ETHUSDT", 1, 2000.0, 50.0, 1000, "3m", 1.0);

        // DCA at KC lower
        let events = engine.process_candle("ETHUSDT", "3m", 2010.0, 1980.0, 1990.0, 2000, 2020.0, 1985.0, 0.0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].exit_reason, "DCA");

        let pos = engine.get_position("ETHUSDT", "3m").unwrap();
        assert_eq!(pos.dca_fills, 1);
        assert!(pos.avg_entry_price < 2000.0); // averaged down

        // TP at KC upper
        let events = engine.process_candle("ETHUSDT", "3m", 2025.0, 1995.0, 2020.0, 3000, 2020.0, 1985.0, 0.0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].exit_reason, "TP");
        assert!(events[0].pnl_usdt > 0.0);
    }

    #[test]
    fn test_pct_hard_stop() {
        let mut engine = TradingEngine::new(test_config());
        engine.process_signal("ETHUSDT", 1, 2000.0, 50.0, 1000, "3m", 1.0);

        // Fill all 4 DCA steps
        for i in 0..4 {
            let low = 1990.0 - (i as f64 * 10.0);
            engine.process_candle("ETHUSDT", "3m", 2010.0, low, 1995.0, 2000 + i * 1000, 2050.0, low, 0.0);
        }
        let pos = engine.get_position("ETHUSDT", "3m").unwrap();
        assert_eq!(pos.dca_fills, 4);

        // Now drop price enough to trigger 2.5% stop
        let avg = engine.get_position("ETHUSDT", "3m").unwrap().avg_entry_price;
        let stop_price = avg * (1.0 - 0.03); // 3% loss > 2.5% threshold
        let events = engine.process_candle("ETHUSDT", "3m", avg, stop_price, stop_price, 10000, 2050.0, stop_price - 10.0, 0.0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].exit_reason, "PCT_STOP");
    }

    #[test]
    fn test_dynamic_compounding() {
        let mut engine = TradingEngine::new(test_config());
        // Balance = $10K → tier1 (<$30K) → 3% → $300 margin
        let margin = engine.get_step_margin(1.0);
        assert!((margin - 300.0).abs() < 0.01);

        // Simulate high balance
        engine.wallet.balance = 50000.0;
        let margin = engine.get_step_margin(1.0);
        // $50K → tier2 (<$150K) → 5% → $2500
        assert!((margin - 2500.0).abs() < 0.01);
    }
}
