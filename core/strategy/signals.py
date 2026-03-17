"""Signal engine — generates LONG/SHORT entry signals using PMax (Profit Maximizer).

PMax crossover logic: MAvg crosses above PMax → LONG, MAvg crosses below PMax → SHORT.

Uses Rust (scalper_engine) indicators for numerical consistency with backtest.
Falls back to Python indicators if Rust engine is not available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# --- Rust engine integration for indicators ---
try:
    import scalper_engine
    _USE_RUST = True
    logger.info("SignalEngine: using Rust indicators (backtest-identical)")
except ImportError:
    _USE_RUST = False
    logger.info("SignalEngine: Rust engine not available, using Python fallback")

from core.strategy.indicators import (
    adaptive_pmax,
    atr,
    ema,
    pmax,
    rsi,
)


@dataclass
class Signal:
    """Represents a trading signal."""
    timestamp: int
    symbol: str
    side: str          # "LONG" or "SHORT"
    price: float
    rsi_value: float
    atr_value: float
    tf_label: str = ""  # timeframe label (e.g. "1m", "3m")
    size_multiplier: float = 1.0  # notional size multiplier for this TF


class SignalEngine:
    """Generates signals for a single symbol using PMax (Profit Maximizer) strategy.

    Buy signal:  MAvg crosses above PMax line
    Sell signal: MAvg crosses below PMax line

    Each instance is bound to a specific timeframe config (1m, 3m, etc.).
    """

    def __init__(self, config: dict, tf_config: dict | None = None) -> None:
        self._cfg = config["strategy"]
        self._trade_type = config["trading"]["trade_type"]

        # Timeframe-specific config (from strategy.timeframes[])
        if tf_config is None:
            # Legacy fallback: single TF mode
            tf_config = self._cfg
        self._tf_config = tf_config
        self._tf_label = tf_config.get("label", "1m")
        self._size_multiplier = tf_config.get("size_multiplier", 1.0)

        # PMax settings (from tf_config or top-level)
        pmax_cfg = tf_config.get("pmax", self._cfg.get("pmax", {}))
        self._pmax_cfg = pmax_cfg  # store full config for adaptive mode
        self._pmax_adaptive = pmax_cfg.get("adaptive", False)
        self._pmax_source = pmax_cfg.get("source", "hl2")
        self._atr_period = pmax_cfg.get("atr_period", 10)
        self._atr_multiplier = pmax_cfg.get("atr_multiplier", 3.0)
        self._ma_type = pmax_cfg.get("ma_type", "EMA")
        self._ma_length = pmax_cfg.get("ma_length", 10)
        self._change_atr = pmax_cfg.get("change_atr", True)
        self._normalize_atr = pmax_cfg.get("normalize_atr", False)

        # Signal filters (from tf_config or top-level)
        filters = tf_config.get("filters", self._cfg.get("filters", {}))
        self._ema_filter = filters.get("ema_trend", {})
        self._rsi_filter = filters.get("rsi", {})
        self._atr_filter = filters.get("atr_volatility", {})

    def _get_source(self, df: pd.DataFrame) -> pd.Series:
        """Get source series based on config (hl2, close, hlc3, ohlc4)."""
        src = self._pmax_source.lower()
        if src == "hl2":
            return (df["high"] + df["low"]) / 2
        elif src == "hlc3":
            return (df["high"] + df["low"] + df["close"]) / 3
        elif src == "ohlc4":
            return (df["open"] + df["high"] + df["low"] + df["close"]) / 4
        else:
            return df["close"]

    def _to_arr(self, series: pd.Series) -> np.ndarray:
        """Convert pandas Series to contiguous float64 numpy array."""
        return np.ascontiguousarray(series.values, dtype=np.float64)

    def _compute_pmax(self, src: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series):
        """Compute PMax using Rust engine (or Python fallback). Returns (pmax_vals, mavg_vals) as numpy arrays."""
        if _USE_RUST:
            pmax_config = {
                "adaptive": self._pmax_adaptive,
                "atr_period": self._atr_period,
                "atr_multiplier": self._atr_multiplier,
                "ma_length": self._ma_length,
            }
            if self._pmax_adaptive:
                pmax_config.update({
                    "vol_lookback": self._pmax_cfg.get("vol_lookback", 580),
                    "flip_window": self._pmax_cfg.get("flip_window", 440),
                    "mult_base": self._pmax_cfg.get("mult_base", 4.0),
                    "mult_scale": self._pmax_cfg.get("mult_scale", 1.25),
                    "ma_base": self._pmax_cfg.get("ma_base", 3),
                    "ma_scale": self._pmax_cfg.get("ma_scale", 5.5),
                    "atr_base": self._pmax_cfg.get("atr_base", 19),
                    "atr_scale": self._pmax_cfg.get("atr_scale", 1.5),
                    "update_interval": self._pmax_cfg.get("update_interval", 29),
                })
            pmax_arr, mavg_arr, _ = scalper_engine.compute_pmax(
                self._to_arr(src), self._to_arr(high),
                self._to_arr(low), self._to_arr(close), pmax_config,
            )
            return np.asarray(pmax_arr), np.asarray(mavg_arr)
        else:
            if self._pmax_adaptive:
                pmax_line, mavg, _ = adaptive_pmax(src, high, low, close, self._pmax_cfg)
            else:
                pmax_line, mavg, _ = pmax(
                    src, high, low, close,
                    atr_period=self._atr_period, atr_multiplier=self._atr_multiplier,
                    ma_type=self._ma_type, ma_length=self._ma_length,
                    change_atr=self._change_atr, normalize_atr=self._normalize_atr,
                )
            return pmax_line.values, mavg.values

    def _compute_rsi(self, close: pd.Series, period: int) -> float:
        """Compute RSI last value using Rust or Python."""
        if _USE_RUST:
            arr = scalper_engine.compute_rsi(self._to_arr(close), period)
            return float(np.asarray(arr)[-1])
        return float(rsi(close, period).iloc[-1])

    def _compute_atr(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
        """Compute ATR (EMA-based) last value using Rust or Python."""
        if _USE_RUST:
            arr = scalper_engine.compute_atr(self._to_arr(high), self._to_arr(low), self._to_arr(close), period)
            return float(np.asarray(arr)[-1])
        return float(atr(high, low, close, period).iloc[-1])

    def _compute_ema_last(self, close: pd.Series, period: int) -> float:
        """Compute EMA last value using Rust or Python."""
        if _USE_RUST:
            arr = scalper_engine.compute_ema(self._to_arr(close), period)
            return float(np.asarray(arr)[-1])
        return float(ema(close, period).iloc[-1])

    def _compute_rsi_ema_last(self, close: pd.Series, rsi_period: int, ema_period: int = 10) -> float:
        """Compute EMA(RSI) last value using Rust or Python."""
        if _USE_RUST:
            rsi_arr = scalper_engine.compute_rsi(self._to_arr(close), rsi_period)
            ema_arr = scalper_engine.compute_ema(np.asarray(rsi_arr), ema_period)
            return float(np.asarray(ema_arr)[-1])
        return float(ema(rsi(close, rsi_period), ema_period).iloc[-1])

    def _compute_atr_series(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> np.ndarray:
        """Compute full ATR series using Rust or Python. Returns numpy array."""
        if _USE_RUST:
            arr = scalper_engine.compute_atr(self._to_arr(high), self._to_arr(low), self._to_arr(close), period)
            return np.asarray(arr)
        return atr(high, low, close, period).values

    def process(self, df: pd.DataFrame) -> Signal | None:
        """Process candle data — PMax crossover signal detection.

        Walks the entire PMax/MAvg history to find crossover events.
        Returns a signal only when the most recent bar triggered a crossover:
            MAvg crosses above PMax → LONG
            MAvg crosses below PMax → SHORT
        """
        if len(df) < 50:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]
        src = self._get_source(df)

        # --- RSI / ATR for filters (Rust) ---
        rsi_val = self._compute_rsi(close, 28)
        atr_val = self._compute_atr(high, low, close, 50)

        last = df.iloc[-1]
        symbol = str(last.get("symbol", ""))
        base_close = float(last["close"])

        # --- Compute PMax (Rust) ---
        pmax_vals, mavg_vals = self._compute_pmax(src, high, low, close)
        n = len(mavg_vals)

        # --- Walk all bars — crossover state machine ---
        condition = 0.0  # 0=flat, 1=LONG, -1=SHORT
        entry_price = 0.0
        entry_time = 0
        last_transition_idx = -1
        times = df["open_time"].values
        closes = df["close"].values

        for i in range(1, n):
            prev_m = mavg_vals[i - 1]
            prev_p = pmax_vals[i - 1]
            curr_m = mavg_vals[i]
            curr_p = pmax_vals[i]

            if np.isnan(prev_m) or np.isnan(prev_p) or np.isnan(curr_m) or np.isnan(curr_p):
                continue

            # MAvg crosses above PMax → LONG
            buy_cross = prev_m <= prev_p and curr_m > curr_p
            # MAvg crosses below PMax → SHORT
            sell_cross = prev_m >= prev_p and curr_m < curr_p

            if buy_cross and condition <= 0.0:
                condition = 1.0
                entry_price = float(closes[i])
                entry_time = int(times[i])
                last_transition_idx = i

            elif sell_cross and condition >= 0.0:
                condition = -1.0
                entry_price = float(closes[i])
                entry_time = int(times[i])
                last_transition_idx = i

        # Only emit signal if transition happened on the LAST bar
        if last_transition_idx != n - 1:
            return None

        if condition == 1.0 and self._trade_type in ("LONG", "BOTH"):
            if not self._apply_filters(df, "LONG", rsi_val, atr_val):
                logger.info("[FILTERED] %s LONG signal blocked by filters", symbol)
                return None
            return Signal(
                timestamp=entry_time,
                symbol=symbol,
                side="LONG",
                price=entry_price,
                rsi_value=rsi_val,
                atr_value=atr_val,
                tf_label=self._tf_label,
                size_multiplier=self._size_multiplier,
            )

        if condition == -1.0 and self._trade_type in ("SHORT", "BOTH"):
            if not self._apply_filters(df, "SHORT", rsi_val, atr_val):
                logger.info("[FILTERED] %s SHORT signal blocked by filters", symbol)
                return None
            return Signal(
                timestamp=entry_time,
                symbol=symbol,
                side="SHORT",
                price=entry_price,
                rsi_value=rsi_val,
                atr_value=atr_val,
                tf_label=self._tf_label,
                size_multiplier=self._size_multiplier,
            )

        return None

    def process_backfill(self, df: pd.DataFrame) -> Signal | None:
        """Replay full PMax crossover history to find the currently-active position.

        Uses completed bars to determine the current state (LONG/SHORT/flat).
        """
        if len(df) < 50:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]
        src = self._get_source(df)

        rsi_val = self._compute_rsi(close, 28)
        atr_val = self._compute_atr(high, low, close, 50)

        last = df.iloc[-1]
        symbol = str(last.get("symbol", ""))

        # --- Compute PMax (Rust) ---
        pmax_vals, mavg_vals = self._compute_pmax(src, high, low, close)
        n = len(mavg_vals)
        times = df["open_time"].values
        closes = df["close"].values

        condition = 0.0
        entry_price = 0.0
        entry_time = 0

        for i in range(1, n):
            prev_m = mavg_vals[i - 1]
            prev_p = pmax_vals[i - 1]
            curr_m = mavg_vals[i]
            curr_p = pmax_vals[i]

            if np.isnan(prev_m) or np.isnan(prev_p) or np.isnan(curr_m) or np.isnan(curr_p):
                continue

            buy_cross = prev_m <= prev_p and curr_m > curr_p
            sell_cross = prev_m >= prev_p and curr_m < curr_p

            if buy_cross and condition <= 0.0:
                condition = 1.0
                entry_price = float(closes[i])
                entry_time = int(times[i])

            elif sell_cross and condition >= 0.0:
                condition = -1.0
                entry_price = float(closes[i])
                entry_time = int(times[i])

        # Backfill always returns the last crossover state — no filters applied.
        if condition == 1.0 and self._trade_type in ("LONG", "BOTH"):
            sig = Signal(
                timestamp=entry_time,
                symbol=symbol,
                side="LONG",
                price=entry_price,
                rsi_value=rsi_val,
                atr_value=atr_val,
                tf_label=self._tf_label,
                size_multiplier=self._size_multiplier,
            )
            logger.info(
                "[BACKFILL] %s LONG [%s] entry=%.4f rsi=%.2f atr=%.4f bars=%d",
                symbol, self._tf_label, entry_price, rsi_val, atr_val, n,
            )
            return sig

        if condition == -1.0 and self._trade_type in ("SHORT", "BOTH"):
            sig = Signal(
                timestamp=entry_time,
                symbol=symbol,
                side="SHORT",
                price=entry_price,
                rsi_value=rsi_val,
                atr_value=atr_val,
                tf_label=self._tf_label,
                size_multiplier=self._size_multiplier,
            )
            logger.info(
                "[BACKFILL] %s SHORT [%s] entry=%.4f rsi=%.2f atr=%.4f bars=%d",
                symbol, self._tf_label, entry_price, rsi_val, atr_val, n,
            )
            return sig

        return None

    def _apply_filters(self, df: pd.DataFrame, side: str, rsi_val: float, atr_val: float) -> bool:
        """Apply signal filters. Returns True if signal passes all filters."""
        close = df["close"]

        # --- EMA Trend Filter ---
        if self._ema_filter.get("enabled", False):
            period = self._ema_filter.get("period", 144)
            if len(close) >= period:
                ema_val = self._compute_ema_last(close, period)
                current_close = float(close.iloc[-1])
                if side == "LONG" and current_close < ema_val:
                    logger.debug("FILTER: LONG blocked — close %.4f < EMA(%d) %.4f",
                                 current_close, period, ema_val)
                    return False
                if side == "SHORT" and current_close > ema_val:
                    logger.debug("FILTER: SHORT blocked — close %.4f > EMA(%d) %.4f",
                                 current_close, period, ema_val)
                    return False

        # --- RSI Filter ---
        if self._rsi_filter.get("enabled", False):
            ob = self._rsi_filter.get("overbought", 65)
            os_ = self._rsi_filter.get("oversold", 35)
            rsi_ema = self._compute_rsi_ema_last(close, self._rsi_filter.get("period", 28), 10)
            if side == "LONG" and rsi_val > ob and rsi_val > rsi_ema:
                logger.debug("FILTER: LONG blocked — RSI %.2f > OB %d", rsi_val, ob)
                return False
            if side == "SHORT" and rsi_val < os_ and rsi_val < rsi_ema:
                logger.debug("FILTER: SHORT blocked — RSI %.2f < OS %d", rsi_val, os_)
                return False

        # --- ATR Volatility Filter ---
        if self._atr_filter.get("enabled", False):
            min_pct = self._atr_filter.get("min_atr_percentile", 20)
            high = df["high"]
            low = df["low"]
            atr_period = self._atr_filter.get("atr_period", 50)
            atr_arr = self._compute_atr_series(high, low, close, atr_period)
            lookback = min(200, len(atr_arr))
            atr_recent = atr_arr[-lookback:]
            valid = atr_recent[~np.isnan(atr_recent)]
            if len(valid) > 0:
                threshold = float(np.percentile(valid, min_pct))
                if atr_val < threshold:
                    logger.debug("FILTER: signal blocked — ATR %.6f < percentile(%d) %.6f",
                                 atr_val, min_pct, threshold)
                    return False

        return True
