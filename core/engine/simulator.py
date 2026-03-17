"""Dry-run simulation — Rust-powered TradingEngine wrapper.

Same external API as before (Trade, Wallet, process_signal, process_candle_with_df, etc.)
but all trading logic delegated to Rust engine via PyO3.

PMax = macro trend. Keltner Channels = micro DCA/TP levels.
  LONG:  Limit BUY @ KC Lower (DCA)  |  Limit SELL @ KC Upper (TP)
  SHORT: Limit SELL @ KC Upper (DCA) |  Limit BUY @ KC Lower (TP)
All DCA/TP = maker fee. Entry + kill switch = taker fee.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.strategy.risk_manager import PositionState
from core.strategy.signals import Signal
from core.strategy.indicators import atr as atr_indicator, keltner_channel

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    id: int
    symbol: str
    side: str
    entry_price: float
    entry_time: int
    exit_price: float
    exit_time: int
    exit_reason: str
    qty_usdt: float
    leverage: int
    pnl_usdt: float
    pnl_percent: float
    fee_usdt: float
    tf_label: str = ""


@dataclass
class Wallet:
    initial_balance: float
    balance: float
    leverage: int
    margin_per_trade: float
    maker_fee: float
    taker_fee: float
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    maker_fees: float = 0.0
    taker_fees: float = 0.0
    peak_balance: float = 0.0


def _build_rust_config(config: dict) -> dict:
    """Convert settings.yaml config to flat dict for Rust PyTradingEngine."""
    trading = config["trading"]
    strategy = config.get("strategy", {})
    dyncomp = strategy.get("dynamic_comp", {})
    hard_stop_cfg = trading.get("hard_stop", {})
    dyn_sl_cfg = trading.get("dynamic_sl", {})
    pct_stop_cfg = trading.get("pct_hard_stop", {})

    # Build dyncomp tiers as list of (max_balance, comp_pct) tuples
    tiers = []
    if dyncomp.get("enabled", False):
        for t in dyncomp.get("tiers", []):
            tiers.append((t["max_balance"], t["comp_pct"]))

    return {
        "initial_balance": trading["initial_balance"],
        "leverage": float(trading["leverage"]),
        "margin_per_trade": trading.get("margin_per_trade", 300.0),
        "maker_fee": trading.get("maker_fee", 0.0002),
        "taker_fee": trading.get("taker_fee", 0.0005),
        "max_dca_steps": trading.get("max_dca_steps", 4),
        "tp_close_pct": trading.get("tp_close_pct", 0.50),
        "dyncomp_enabled": dyncomp.get("enabled", False),
        "dyncomp_tiers": tiers,
        "pct_stop_enabled": pct_stop_cfg.get("enabled", False),
        "pct_stop_loss": pct_stop_cfg.get("loss_pct", 2.5),
        "dyn_sl_enabled": dyn_sl_cfg.get("enabled", False),
        "dyn_sl_atr_mult": dyn_sl_cfg.get("atr_multiplier", 2.5),
        "dyn_sl_tighten": dyn_sl_cfg.get("tighten_on_dca_full", 0.95),
        "hard_stop_enabled": hard_stop_cfg.get("enabled", False),
        "hard_stop_atr_mult": hard_stop_cfg.get("atr_multiplier", 5.0),
    }


def _event_to_trade(event: dict) -> Trade:
    """Convert Rust TradeEvent dict to Python Trade dataclass."""
    return Trade(
        id=event["id"],
        symbol=event["symbol"],
        side=event["side"],
        entry_price=event["entry_price"],
        entry_time=event["entry_time"],
        exit_price=event["exit_price"],
        exit_time=event["exit_time"],
        exit_reason=event["exit_reason"],
        qty_usdt=event["qty_usdt"],
        leverage=event["leverage"],
        pnl_usdt=event["pnl_usdt"],
        pnl_percent=event["pnl_pct"],
        fee_usdt=event["fee_usdt"],
        tf_label=event.get("tf_label", ""),
    )


def _rust_pos_to_python(pos_dict: dict) -> PositionState:
    """Convert Rust position dict to Python PositionState for API compat."""
    ps = PositionState()
    ps.symbol = pos_dict.get("symbol", "")
    ps.side = pos_dict.get("side", "")
    ps.condition = pos_dict.get("condition", 0.0)
    ps.entry_time = pos_dict.get("entry_time", 0)
    ps.initial_entry_price = pos_dict.get("initial_entry_price", 0.0)
    ps.average_entry_price = pos_dict.get("average_entry_price", 0.0)
    ps.entry_price = pos_dict.get("average_entry_price", 0.0)  # backward compat alias
    ps.entry_atr = pos_dict.get("entry_atr", 0.0)
    ps.margin_per_step = pos_dict.get("margin_per_step", 0.0)
    ps.total_position_notional = pos_dict.get("total_position_notional", 0.0)
    ps.total_fills = pos_dict.get("total_fills", 0)
    ps.dca_fills_count = pos_dict.get("dca_fills_count", 0)
    ps.dca_wave_sold = pos_dict.get("dca_wave_sold", 0)
    ps.hard_stop_price = pos_dict.get("hard_stop_price", 0.0)
    ps.pending_dca_price = pos_dict.get("pending_dca_price", 0.0)
    ps.pending_tp_price = pos_dict.get("pending_tp_price", 0.0)
    ps.remaining_qty = 1.0 if pos_dict.get("condition", 0.0) != 0.0 else 0.0
    ps.leverage = 25  # from config, set by caller if needed
    return ps


class Simulator:
    """Rust-powered dry-run simulator. Same API as Python version."""

    def __init__(self, config: dict) -> None:
        from rust_engine import PyTradingEngine

        trading = config["trading"]
        self._config = config
        self._engine = PyTradingEngine(_build_rust_config(config))

        # Keltner settings (for indicator computation in Python)
        tf_configs = config["strategy"].get("timeframes", [])
        kc_cfg = tf_configs[0].get("keltner", {}) if tf_configs else {}
        self._kc_length = kc_cfg.get("length", 16)
        self._kc_multiplier = kc_cfg.get("multiplier", 1.3)
        self._kc_atr_period = kc_cfg.get("atr_period", 13)

        # Dynamic SL ATR period (for indicator computation)
        dyn_sl_cfg = trading.get("dynamic_sl", {})
        self._dyn_sl_enabled = dyn_sl_cfg.get("enabled", False)
        self._dyn_sl_atr_period = dyn_sl_cfg.get("atr_period", 12)

        # Wallet (synced from Rust after each operation)
        maker = trading.get("maker_fee", 0.0002)
        taker = trading.get("taker_fee", 0.0005)
        initial = trading["initial_balance"]
        self.wallet = Wallet(
            initial_balance=initial,
            balance=initial,
            leverage=trading["leverage"],
            margin_per_trade=trading.get("margin_per_trade", 300.0),
            maker_fee=maker,
            taker_fee=taker,
            peak_balance=initial,
        )

        self.trades: list[Trade] = []

    def _sync_wallet(self) -> None:
        """Pull wallet state from Rust engine."""
        w = self._engine.get_wallet()
        self.wallet.balance = w["balance"]
        self.wallet.peak_balance = w["peak_balance"]
        self.wallet.total_trades = w["total_trades"]
        self.wallet.winning_trades = w["winning_trades"]
        self.wallet.losing_trades = w["losing_trades"]
        self.wallet.total_pnl = w["total_pnl"]
        self.wallet.total_fees = w["total_fees"]
        self.wallet.maker_fees = w["maker_fees"]
        self.wallet.taker_fees = w["taker_fees"]

    @staticmethod
    def _pos_key(symbol: str, tf_label: str) -> str:
        return f"{symbol}:{tf_label}" if tf_label else symbol

    @property
    def positions(self) -> dict[str, PositionState]:
        """Return positions dict compatible with existing API (server.py chart-data)."""
        rust_positions = self._engine.get_all_positions()
        result = {}
        for key, pos_dict in rust_positions.items():
            result[key] = _rust_pos_to_python(pos_dict)
        return result

    def has_position(self, symbol: str, tf_label: str = "") -> bool:
        return self._engine.has_position(symbol, tf_label)

    def has_any_position(self, symbol: str) -> bool:
        positions = self._engine.get_all_positions()
        for key in positions:
            if key == symbol or key.startswith(symbol + ":"):
                return True
        return False

    def process_signal(self, signal: Signal, entry_time: int = 0) -> list[Trade]:
        """PMax crossover → delegate to Rust engine."""
        side = 1 if signal.side == "LONG" else -1
        size_mult = signal.size_multiplier if signal.size_multiplier > 0 else 1.0
        ts = entry_time or signal.timestamp

        events = self._engine.process_signal(
            signal.symbol, side, signal.price, signal.atr_value,
            ts, signal.tf_label or "", size_mult,
        )

        result = []
        for e in events:
            trade = _event_to_trade(e)
            self.trades.append(trade)
            result.append(trade)

        self._sync_wallet()
        return result

    def process_candle_with_df(self, symbol: str, df: pd.DataFrame,
                                tf_label: str = "") -> list[Trade]:
        """Process one candle — compute KC in Python, delegate logic to Rust."""
        if not self._engine.has_position(symbol, tf_label):
            return []

        if len(df) < max(self._kc_length, self._kc_atr_period) + 1:
            return []

        candle_high = float(df["high"].iloc[-1])
        candle_low = float(df["low"].iloc[-1])
        candle_close = float(df["close"].iloc[-1])
        close_time = int(df["open_time"].iloc[-1])

        # Compute Keltner Channel bands
        kc_mid, kc_upper, kc_lower = keltner_channel(
            df["high"], df["low"], df["close"],
            kc_length=self._kc_length,
            kc_multiplier=self._kc_multiplier,
            atr_period=self._kc_atr_period,
        )
        upper_val = float(kc_upper.iloc[-1])
        lower_val = float(kc_lower.iloc[-1])
        if np.isnan(upper_val) or np.isnan(lower_val):
            return []

        # Compute Dynamic SL ATR if enabled
        dyn_sl_atr = 0.0
        if self._dyn_sl_enabled and len(df) > self._dyn_sl_atr_period:
            atr_series = atr_indicator(df["high"], df["low"], df["close"],
                                       self._dyn_sl_atr_period)
            val = float(atr_series.iloc[-1])
            if not np.isnan(val) and val > 0:
                dyn_sl_atr = val

        # Delegate to Rust engine
        events = self._engine.process_candle(
            symbol, tf_label,
            candle_high, candle_low, candle_close, close_time,
            upper_val, lower_val, dyn_sl_atr,
        )

        result = []
        for e in events:
            trade = _event_to_trade(e)
            self.trades.append(trade)
            result.append(trade)

        self._sync_wallet()
        return result

    def process_candle(self, symbol: str, high: float, low: float, close_time: int,
                       tf_label: str = "", candle_close: float = 0.0) -> list[Trade]:
        """Stub — use process_candle_with_df for Keltner (needs full DataFrame)."""
        return []

    def get_stats(self) -> dict[str, Any]:
        """Return stats dict — same shape as before."""
        self._sync_wallet()
        return self._engine.get_stats()
