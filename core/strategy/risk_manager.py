"""Keltner Channel DCA + TP risk manager with Dynamic Compounding.

PMax determines macro trend. Keltner Channels determine DCA/TP levels:
  LONG:  Limit BUY  at KC Lower Band (DCA)  |  Limit SELL at KC Upper Band (TP)
  SHORT: Limit SELL at KC Upper Band (DCA)  |  Limit BUY  at KC Lower Band (TP)

Dynamic Compounding (Kelly):
  Balance < $30K   → comp_pct = 3.0%  (kontrollü)
  $30K - $150K     → comp_pct = 5.0%  (büyüme)
  $150K+           → comp_pct = 2.25% (muhafazakâr)

  step_margin = balance * comp_pct / 100

Dynamic Stop Loss (DYN_SL): Close-based ATR stop.
  - LONG: close <= avg_entry - sl_mult * ATR(sl_period) -> exit
  - SHORT: close >= avg_entry + sl_mult * ATR(sl_period) -> exit
  - DCA full: multiply sl_mult by tighten factor (tighter stop)
  - Hard stop 5x ATR(11) remains as emergency backup (candle H/L based)

Every new candle: cancel unfilled orders, re-place at updated Keltner levels.
All DCA/TP orders are Post-Only (GTX) = Maker fee guaranteed.
Kill switch on PMax reversal: cancel all limits, market close.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    symbol: str = ""
    side: str = ""                     # "LONG" or "SHORT"
    condition: float = 0.0             # 1.0=LONG, -1.0=SHORT, 0.0=flat
    entry_time: int = 0

    # Position tracking
    initial_entry_price: float = 0.0
    average_entry_price: float = 0.0
    entry_atr: float = 0.0
    margin_per_step: float = 0.0
    leverage: int = 1
    total_position_notional: float = 0.0
    total_fills: int = 0
    dca_fills_count: int = 0           # current wave DCA count (resets after all TPs sold)
    dca_wave_sold: int = 0             # how many TPs sold in current wave

    # Stop Loss
    hard_stop_price: float = 0.0       # 5x ATR emergency backup (fixed at entry, H/L based)

    # Dynamic order tracking (for live executor)
    pending_dca_order_id: int = 0      # current open DCA limit order
    pending_tp_order_id: int = 0       # current open TP limit order
    pending_dca_price: float = 0.0     # current DCA limit price
    pending_tp_price: float = 0.0      # current TP limit price

    # Backward compat
    remaining_qty: float = 1.0
    entry_price: float = 0.0

    # Legacy fields (for frontend compat)
    dca_levels: list = field(default_factory=list)
    tp_price: float = 0.0
    tp_order_id: int = 0
    tp_armed: bool = True


def get_dynamic_comp_pct(balance: float, tiers: list[dict]) -> float:
    """Get compounding percentage based on current balance and tier config."""
    if not tiers:
        return 10.0  # default fallback
    for tier in tiers:
        if balance < tier.get("max_balance", float("inf")):
            return tier.get("comp_pct", 10.0)
    return tiers[-1].get("comp_pct", 2.0)


def calc_step_margin(balance: float, comp_pct: float) -> float:
    """Calculate step margin from balance and comp percentage."""
    return balance * comp_pct / 100.0


class RiskManager:
    """Keltner Channel DCA + TP manager with Dynamic Compounding and Hard Stop."""

    def __init__(self, config: dict, risk_config: dict | None = None) -> None:
        trading = config.get("trading", {})
        self._margin_per_trade = trading.get("margin_per_trade", 100.0)
        self._leverage = trading.get("leverage", 25)

        # Dynamic compounding config
        strategy = config.get("strategy", {})
        dyncomp = strategy.get("dynamic_comp", {})
        self._dyncomp_enabled = dyncomp.get("enabled", False)
        self._dyncomp_tiers = dyncomp.get("tiers", [])

        # Risk params from config
        self._max_dca_steps = trading.get("max_dca_steps", 1)
        self._tp_close_pct = trading.get("tp_close_pct", 0.05)

        # Graduated DCA multipliers [0.50, 3.00, 3.75, 3.75]
        self._dca_step_multipliers = trading.get("dca_step_multipliers", [])
        # Graduated TP pcts [0.50, 0.80, 0.85, 0.90]
        self._tp_step_pcts = trading.get("tp_step_pcts", [])

        # Hard stop (emergency backup — fixed at entry, H/L based)
        hard_stop_cfg = trading.get("hard_stop", {})
        self._hard_stop_enabled = hard_stop_cfg.get("enabled", False)
        self._hard_stop_atr_mult = hard_stop_cfg.get("atr_multiplier", 5.0)
        self._hard_stop_atr_period = hard_stop_cfg.get("atr_period", 11)

        # Dynamic SL (close-based, tightens after DCA full)
        dyn_sl_cfg = trading.get("dynamic_sl", {})
        self._dyn_sl_enabled = dyn_sl_cfg.get("enabled", False)
        self._dyn_sl_atr_mult = dyn_sl_cfg.get("atr_multiplier", 2.5)
        self._dyn_sl_atr_period = dyn_sl_cfg.get("atr_period", 12)
        self._dyn_sl_tighten = dyn_sl_cfg.get("tighten_on_dca_full", 0.95)

        # Percentage-based hard stop (triggers after DCA full)
        pct_stop_cfg = trading.get("pct_hard_stop", {})
        self._pct_stop_enabled = pct_stop_cfg.get("enabled", False)
        self._pct_stop_loss = pct_stop_cfg.get("loss_pct", 2.5)  # % loss threshold

    def get_step_margin(self, balance: float) -> float:
        """Calculate step margin using dynamic compounding or fixed margin."""
        if self._dyncomp_enabled and self._dyncomp_tiers:
            comp_pct = get_dynamic_comp_pct(balance, self._dyncomp_tiers)
            margin = calc_step_margin(balance, comp_pct)
            logger.debug(
                "[DYNCOMP] balance=%.2f comp_pct=%.1f%% step_margin=%.2f",
                balance, comp_pct, margin,
            )
            return margin
        return self._margin_per_trade

    def open_position(
        self, symbol: str, side: str, entry_price: float, atr_value: float,
        margin_per_trade: float | None = None, leverage: int | None = None,
    ) -> PositionState:
        """Create new position on PMax crossover (market order)."""
        margin = margin_per_trade or self._margin_per_trade
        lev = leverage or self._leverage
        notional = margin * lev

        # Calculate hard stop price
        hard_stop_price = 0.0
        if self._hard_stop_enabled and atr_value > 0:
            stop_distance = self._hard_stop_atr_mult * atr_value
            if side == "LONG":
                hard_stop_price = entry_price - stop_distance
            else:
                hard_stop_price = entry_price + stop_distance

        pos = PositionState(
            symbol=symbol, side=side,
            condition=1.0 if side == "LONG" else -1.0,
            initial_entry_price=entry_price,
            average_entry_price=entry_price,
            entry_price=entry_price,
            entry_atr=atr_value,
            margin_per_step=margin,
            leverage=lev,
            total_position_notional=notional,
            total_fills=1,
            dca_fills_count=0,
            remaining_qty=1.0,
            hard_stop_price=hard_stop_price,
        )

        logger.info(
            "[ENTRY] %s %s @ %.4f | notional=%.2f | hard_stop=%.4f",
            symbol, side, entry_price, notional, hard_stop_price,
        )
        return pos

    def check_dynamic_sl(
        self, pos: PositionState, candle_close: float, current_atr: float,
    ) -> tuple[bool, float]:
        """Dynamic SL: close-based ATR stop. Tightens when DCA is full.

        LONG:  close <= avg_entry - mult * ATR -> exit
        SHORT: close >= avg_entry + mult * ATR -> exit

        Returns (triggered, sl_price).
        """
        if not self._dyn_sl_enabled or pos.condition == 0.0 or current_atr <= 0:
            return False, 0.0

        mult = self._dyn_sl_atr_mult
        if pos.dca_fills_count >= self._max_dca_steps:
            mult *= self._dyn_sl_tighten

        sl_dist = mult * current_atr

        if pos.side == "LONG":
            sl_price = pos.average_entry_price - sl_dist
            if candle_close <= sl_price:
                return True, sl_price
        else:  # SHORT
            sl_price = pos.average_entry_price + sl_dist
            if candle_close >= sl_price:
                return True, sl_price

        return False, 0.0

    def check_pct_hard_stop(
        self, pos: PositionState, candle_close: float,
    ) -> tuple[bool, float]:
        """Percentage-based hard stop — triggers only after all DCA steps are filled.

        LONG:  loss_pct = (avg_entry - close) / avg_entry * 100
        SHORT: loss_pct = (close - avg_entry) / avg_entry * 100
        If loss_pct >= threshold → exit at candle close.

        Returns (triggered, exit_price).
        """
        if not self._pct_stop_enabled or pos.condition == 0.0:
            return False, 0.0

        # Only trigger after all DCA steps are filled
        if pos.dca_fills_count < self._max_dca_steps:
            return False, 0.0

        avg = pos.average_entry_price
        if avg <= 0:
            return False, 0.0

        if pos.side == "LONG":
            loss_pct = (avg - candle_close) / avg * 100
        else:
            loss_pct = (candle_close - avg) / avg * 100

        if loss_pct >= self._pct_stop_loss:
            logger.info(
                "[PCT_STOP] %s %s loss=%.2f%% >= %.2f%% | avg=%.4f close=%.4f",
                pos.symbol, pos.side, loss_pct, self._pct_stop_loss, avg, candle_close,
            )
            return True, candle_close

        return False, 0.0

    def check_hard_stop(
        self, pos: PositionState, candle_high: float, candle_low: float,
    ) -> tuple[bool, float, str]:
        """Check if hard stop was hit (emergency backup, H/L based).

        Returns (triggered, fill_price, reason).
        """
        if not self._hard_stop_enabled or pos.condition == 0.0:
            return False, 0.0, ""

        if pos.hard_stop_price > 0:
            if pos.side == "LONG" and candle_low <= pos.hard_stop_price:
                return True, pos.hard_stop_price, "HARD_STOP"
            if pos.side == "SHORT" and candle_high >= pos.hard_stop_price:
                return True, pos.hard_stop_price, "HARD_STOP"

        return False, 0.0, ""

    def update_hard_stop(self, pos: PositionState, atr_value: float) -> None:
        """Recalculate hard stop after DCA fill (new avg entry)."""
        if not self._hard_stop_enabled or atr_value <= 0:
            return
        stop_distance = self._hard_stop_atr_mult * atr_value
        if pos.side == "LONG":
            pos.hard_stop_price = pos.average_entry_price - stop_distance
        else:
            pos.hard_stop_price = pos.average_entry_price + stop_distance

    def check_keltner_signals(
        self, pos: PositionState,
        candle_high: float, candle_low: float, candle_close: float,
        kc_upper: float, kc_lower: float,
    ) -> tuple[str, float]:
        """Check if Keltner band was touched during this candle.

        Simulates limit orders sitting at KC bands:
          LONG DCA:  limit buy  @ KC Lower → candle low touches lower band
          LONG TP:   limit sell @ KC Upper → candle high touches upper band
          SHORT DCA: limit sell @ KC Upper → candle high touches upper band
          SHORT TP:  limit buy  @ KC Lower → candle low touches lower band

        Returns ("DCA"|"TP"|"", fill_price).
        Priority: DCA first (buy the dip), then TP (sell the rip).
        """
        if pos.condition == 0.0:
            return "", 0.0

        if pos.side == "LONG":
            # DCA: price dips to KC Lower Band (max per wave)
            if pos.dca_fills_count < self._max_dca_steps and candle_low <= kc_lower:
                return "DCA", kc_lower

            # TP: price rises to KC Upper Band (only if we have DCA to unload)
            if pos.dca_fills_count > 0 and candle_high >= kc_upper:
                return "TP", kc_upper

        else:  # SHORT
            # DCA: price rises to KC Upper Band (max per wave)
            if pos.dca_fills_count < self._max_dca_steps and candle_high >= kc_upper:
                return "DCA", kc_upper

            # TP: price dips to KC Lower Band
            if pos.dca_fills_count > 0 and candle_low <= kc_lower:
                return "TP", kc_lower

        return "", 0.0

    def get_dca_multiplier(self, dca_step: int) -> float:
        """Get graduated DCA margin multiplier for given step (0-indexed)."""
        if self._dca_step_multipliers and dca_step < len(self._dca_step_multipliers):
            return self._dca_step_multipliers[dca_step]
        return 1.0

    def get_tp_close_pct(self, dca_fills: int) -> float:
        """Get graduated TP close percentage for given DCA fill count (1-indexed)."""
        if self._tp_step_pcts and dca_fills > 0 and dca_fills <= len(self._tp_step_pcts):
            return self._tp_step_pcts[dca_fills - 1]
        return self._tp_close_pct

    def process_dca_fill(self, pos: PositionState, fill_price: float) -> None:
        """DCA limit order filled — recalculate average entry with graduated multiplier."""
        dca_mult = self.get_dca_multiplier(pos.dca_fills_count)
        step_notional = pos.margin_per_step * pos.leverage * dca_mult
        old_total = pos.total_position_notional
        new_total = old_total + step_notional

        pos.average_entry_price = (
            (pos.average_entry_price * old_total + fill_price * step_notional) / new_total
        )
        pos.entry_price = pos.average_entry_price
        pos.total_position_notional = new_total
        pos.total_fills += 1
        pos.dca_fills_count += 1
        pos.dca_wave_sold = 0  # reset sell counter — new buys in this wave

        logger.info(
            "[DCA] %s fill @ %.4f | avg=%.4f | notional=%.2f | dca=%d/%d",
            pos.symbol, fill_price, pos.average_entry_price,
            pos.total_position_notional, pos.dca_fills_count, self._max_dca_steps,
        )

    def process_tp_fill(self, pos: PositionState, fill_price: float) -> float:
        """TP limit order filled — close graduated pct of position. Returns closed notional."""
        tp_pct = self.get_tp_close_pct(pos.dca_fills_count)
        closed_notional = pos.total_position_notional * tp_pct
        pos.total_position_notional -= closed_notional
        pos.dca_fills_count = max(0, pos.dca_fills_count - 1)
        pos.dca_wave_sold += 1
        pos.total_fills = max(1, pos.total_fills - 1)

        # All DCAs in this wave sold → reset for next wave
        if pos.dca_fills_count == 0:
            pos.dca_wave_sold = 0

        pos.remaining_qty = (
            pos.total_position_notional / (pos.margin_per_step * pos.leverage)
            if pos.total_position_notional > 0 else 0.0
        )

        if pos.total_position_notional < 1.0:
            pos.condition = 0.0
            pos.remaining_qty = 0.0
            pos.total_position_notional = 0.0

        logger.info(
            "[TP] %s closed %.2f @ %.4f | remaining=%.2f | dca=%d/%d",
            pos.symbol, closed_notional, fill_price,
            pos.total_position_notional, pos.dca_fills_count, self._max_dca_steps,
        )
        return closed_notional

    def get_grid_info(self, pos: PositionState) -> dict[str, Any]:
        """Return state for API/frontend."""
        return {
            "initial_entry": pos.initial_entry_price,
            "average_entry": pos.average_entry_price,
            "entry_atr": pos.entry_atr,
            "total_notional": pos.total_position_notional,
            "total_fills": pos.total_fills,
            "dca_fills_count": pos.dca_fills_count,
            "max_dca_steps": self._max_dca_steps,
            "pending_dca_price": pos.pending_dca_price,
            "pending_tp_price": pos.pending_tp_price,
            "tp_price": pos.pending_tp_price,
            "hard_stop_price": pos.hard_stop_price,
            "dca_levels": [],
        }
