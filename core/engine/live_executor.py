"""Live execution engine — real Binance Futures orders.

Same signal processing pipeline as dry-run Simulator, but:
- Entry/Reversal → market orders via API
- SL → stop-market order on exchange (protection)
- TP monitoring → candle-based (same as dry-run), partial close via market order
- Per-pair config: each pair has its own margin, leverage
- Order fill verification — polls Binance to confirm actual avgPrice
- Account protection — max drawdown circuit breaker, max total margin check
- Position sync — periodically reconciles with Binance exchange positions

State machine:
  OBSERVING → first start, shows existing positions, waits for first signal
  ACTIVE    → after first signal, places real orders
  STOPPED   → circuit breaker triggered, no new trades
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.data.binance_futures import BinanceFutures
from core.strategy.risk_manager import (
    PositionState, RiskManager, get_dynamic_comp_pct, calc_step_margin,
)
from core.strategy.signals import Signal

logger = logging.getLogger("live_executor")


class PairState(str, Enum):
    """Per-pair operational state."""
    OBSERVING = "OBSERVING"  # watching, not trading yet
    ACTIVE = "ACTIVE"        # live trading enabled
    STOPPED = "STOPPED"      # circuit breaker triggered


@dataclass
class PairConfig:
    """Per-pair trading configuration."""
    symbol: str
    margin: float        # USDT margin per trade
    leverage: int        # leverage multiplier
    enabled: bool = True


@dataclass
class LiveTrade:
    """Completed live trade record."""
    id: int
    symbol: str
    side: str
    entry_price: float
    entry_time: int
    exit_price: float
    exit_time: int
    exit_reason: str
    qty: float            # base asset quantity
    qty_usdt: float       # notional value
    leverage: int
    pnl_usdt: float
    pnl_percent: float
    fee_usdt: float
    order_id: int = 0


class LiveExecutor:
    """Live trading engine with per-pair config and observe-first logic.

    Supports dual-timeframe: positions keyed by "symbol:tf_label".
    Per-TF risk managers for independent TP/SL levels.
    """

    def __init__(self, client: BinanceFutures, config: dict) -> None:
        self._client = client
        self._config = config

        # Build per-TF risk managers
        self._risk_managers: dict[str, RiskManager] = {}
        tf_configs = config["strategy"].get("timeframes", [])
        if tf_configs:
            for tf_cfg in tf_configs:
                label = tf_cfg.get("label", "1m")
                risk_cfg = tf_cfg.get("risk")
                if risk_cfg:
                    self._risk_managers[label] = RiskManager(config, risk_config=risk_cfg)
                else:
                    self._risk_managers[label] = RiskManager(config)
        self._default_risk_mgr = RiskManager(config)

        # Per-pair state
        self._pair_configs: dict[str, PairConfig] = {}
        self._pair_states: dict[str, PairState] = {}

        # Active positions: "symbol:tf_label" → PositionState
        self._positions: dict[str, PositionState] = {}
        # Track real quantities on exchange: "symbol:tf_label" → base qty
        self._position_qty: dict[str, float] = {}
        # Track size multiplier per position key
        self._size_multipliers: dict[str, float] = {}
        # Track tf_label per position key
        self._tf_labels: dict[str, str] = {}
        # SL order IDs on exchange
        self._sl_order_ids: dict[str, int] = {}

        # Last processed signal timestamp per position key
        self._last_signal_ts: dict[str, int] = {}

        # Trade history
        self.trades: list[LiveTrade] = []
        self._trade_counter = 0

        # Wallet tracking (from Binance)
        self.balance: float = 0.0
        self.available_balance: float = 0.0
        self._initial_balance: float = 0.0

        # Fee rates
        trading = config.get("trading", {})
        self._maker_fee = trading.get("maker_fee", 0.0002)
        self._taker_fee = trading.get("taker_fee", 0.0005)
        self._base_margin = trading.get("margin_per_trade", 100.0)

        # Dynamic compounding config
        strategy = config.get("strategy", {})
        dyncomp = strategy.get("dynamic_comp", {})
        self._dyncomp_enabled = dyncomp.get("enabled", False)
        self._dyncomp_tiers = dyncomp.get("tiers", [])

        # Hard stop config
        hard_stop_cfg = trading.get("hard_stop", {})
        self._hard_stop_enabled = hard_stop_cfg.get("enabled", False)

        # Percentage-based hard stop config
        pct_stop_cfg = trading.get("pct_hard_stop", {})
        self._pct_stop_enabled = pct_stop_cfg.get("enabled", False)

        # ── Account Protection ──
        protection = config.get("protection", {})
        self._max_drawdown_pct: float = protection.get("max_drawdown_pct", 10.0)
        self._max_total_margin_pct: float = protection.get("max_total_margin_pct", 50.0)
        self._max_open_positions: int = protection.get("max_open_positions", 5)
        self.circuit_breaker_triggered: bool = False
        self.circuit_breaker_reason: str = ""

        # ── Position Sync ──
        self._last_sync_ts: float = 0.0
        self._sync_interval: float = 60.0
        self.sync_warnings: list[str] = []

        # Stats
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_fees: float = 0.0

    @staticmethod
    def _pos_key(symbol: str, tf_label: str) -> str:
        return f"{symbol}:{tf_label}" if tf_label else symbol

    def _get_risk_mgr(self, tf_label: str) -> RiskManager:
        return self._risk_managers.get(tf_label, self._default_risk_mgr)

    @property
    def positions(self) -> dict[str, PositionState]:
        return self._positions

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def configure_pair(self, symbol: str, margin: float, leverage: int) -> None:
        """Configure a pair for live trading."""
        self._pair_configs[symbol] = PairConfig(
            symbol=symbol, margin=margin, leverage=leverage,
        )
        self._pair_states[symbol] = PairState.OBSERVING

        # Set leverage on exchange
        try:
            self._client.set_leverage(symbol, leverage)
            logger.info("[SETUP] %s leverage set to %dx", symbol, leverage)
        except Exception as e:
            logger.error("[SETUP] Failed to set leverage for %s: %s", symbol, e)

        # Set margin type to ISOLATED
        try:
            self._client.set_margin_type(symbol, "ISOLATED")
            logger.info("[SETUP] %s margin type set to ISOLATED", symbol)
        except Exception as e:
            logger.warning("[SETUP] Margin type for %s: %s", symbol, e)

    def refresh_balance(self) -> dict[str, float]:
        """Fetch latest USDT balance from Binance."""
        bal = self._client.get_balance()
        self.balance = bal["balance"]
        self.available_balance = bal["available"]
        # Capture initial balance on first call (for drawdown calc)
        if self._initial_balance == 0.0 and self.balance > 0:
            self._initial_balance = self.balance
        return bal

    def load_exchange_positions(self) -> list[dict[str, Any]]:
        """Load existing open positions from Binance.

        Called on start to show what's already open.
        Does NOT create PositionState — just returns raw data for display.
        """
        return self._client.get_positions()

    def has_position(self, symbol: str, tf_label: str = "") -> bool:
        key = self._pos_key(symbol, tf_label)
        return key in self._positions and self._positions[key].condition != 0.0

    def is_observing(self, symbol: str) -> bool:
        return self._pair_states.get(symbol) == PairState.OBSERVING

    def is_active(self, symbol: str) -> bool:
        return self._pair_states.get(symbol) == PairState.ACTIVE

    def activate_pair(self, symbol: str) -> None:
        """Transition pair from OBSERVING to ACTIVE."""
        self._pair_states[symbol] = PairState.ACTIVE
        logger.info("[STATE] %s → ACTIVE (live trading enabled)", symbol)

    # ------------------------------------------------------------------
    # Order Fill Verification
    # ------------------------------------------------------------------

    def _verify_fill(self, symbol: str, result: dict, fallback_price: float) -> float:
        """Verify order fill price from Binance. Returns actual avgPrice.

        1. Check avgPrice in order response
        2. If missing/zero, poll order by orderId
        3. Last resort: use fallback_price and log warning
        """
        # Step 1: Check immediate response
        avg_price = float(result.get("avgPrice", 0))
        if avg_price > 0:
            return avg_price

        # Step 2: Poll order for fill confirmation
        order_id = result.get("orderId")
        if order_id:
            verified_price = self._client.get_order_fill_price(symbol, int(order_id))
            if verified_price > 0:
                return verified_price

        # Step 3: Fallback — log warning
        logger.warning(
            "[FILL] Could not verify fill for %s orderId=%s — using fallback %.4f",
            symbol, result.get("orderId"), fallback_price,
        )
        return fallback_price

    # ------------------------------------------------------------------
    # Account Protection
    # ------------------------------------------------------------------

    def _check_account_protection(self, symbol: str) -> str | None:
        """Check account protection rules before opening a new position.

        Returns None if OK, or a reason string if blocked.
        """
        # Circuit breaker already triggered
        if self.circuit_breaker_triggered:
            return f"Circuit breaker active: {self.circuit_breaker_reason}"

        # 1. Max drawdown check
        if self._initial_balance > 0:
            drawdown_pct = (self._initial_balance - self.balance) / self._initial_balance * 100
            if drawdown_pct >= self._max_drawdown_pct:
                self.circuit_breaker_triggered = True
                self.circuit_breaker_reason = (
                    f"Max drawdown {self._max_drawdown_pct}% reached "
                    f"(current: {drawdown_pct:.2f}%)"
                )
                logger.critical("[PROTECTION] %s", self.circuit_breaker_reason)
                return self.circuit_breaker_reason

        # 2. Max concurrent positions check
        open_count = sum(
            1 for pos in self._positions.values() if pos.condition != 0.0
        )
        if open_count >= self._max_open_positions:
            reason = (
                f"Max open positions ({self._max_open_positions}) reached — "
                f"currently {open_count} open"
            )
            logger.warning("[PROTECTION] %s — skipping %s", reason, symbol)
            return reason

        # 3. Max total margin check
        pc = self._pair_configs.get(symbol)
        if pc and self._initial_balance > 0:
            # Sum margin of all open positions + this new one
            total_margin = pc.margin  # the new trade
            for key, pos in self._positions.items():
                if pos.condition != 0.0:
                    pos_symbol = pos.symbol
                    pos_pc = self._pair_configs.get(pos_symbol)
                    pos_mult = self._size_multipliers.get(key, 1.0)
                    if pos_pc:
                        total_margin += pos_pc.margin * pos_mult * pos.remaining_qty

            margin_pct = total_margin / self._initial_balance * 100
            if margin_pct > self._max_total_margin_pct:
                reason = (
                    f"Total margin {margin_pct:.1f}% exceeds limit "
                    f"{self._max_total_margin_pct}% — skipping {symbol}"
                )
                logger.warning("[PROTECTION] %s", reason)
                return reason

        return None

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker (after user review)."""
        self.circuit_breaker_triggered = False
        self.circuit_breaker_reason = ""
        logger.info("[PROTECTION] Circuit breaker manually reset")

    # ------------------------------------------------------------------
    # Position Sync — reconcile with Binance exchange
    # ------------------------------------------------------------------

    def sync_positions(self) -> list[str]:
        """Compare bot's position tracking with actual Binance positions.

        Returns list of warning messages about any drift detected.
        Note: With dual-TF and hedge mode, multiple bot position keys may
        map to the same exchange symbol. Sync is best-effort.
        """
        now = time.time()
        if now - self._last_sync_ts < self._sync_interval:
            return []
        self._last_sync_ts = now

        warnings: list[str] = []

        try:
            exchange_positions = self._client.get_positions()
        except Exception as e:
            msg = f"[SYNC] Failed to fetch exchange positions: {e}"
            logger.error(msg)
            warnings.append(msg)
            return warnings

        # Build lookup: symbol → exchange position
        exchange_map: dict[str, dict] = {}
        for ep in exchange_positions:
            exchange_map[ep["symbol"]] = ep

        # Check bot positions against exchange
        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition == 0.0:
                continue

            symbol = pos.symbol
            tf_label = self._tf_labels.get(key, "")

            if symbol not in exchange_map:
                msg = (
                    f"[SYNC] {symbol} {pos.side} [{tf_label}]: bot has open position but "
                    f"exchange shows no position — marking as closed"
                )
                logger.warning(msg)
                warnings.append(msg)

                old_sl = self._sl_order_ids.get(key)
                if old_sl:
                    try:
                        self._client.cancel_order(symbol, old_sl)
                    except Exception:
                        pass

                pos.condition = 0.0
                pos.remaining_qty = 0.0
                self._position_qty.pop(key, None)
                self._sl_order_ids.pop(key, None)

        # Check 5: Verify SL orders still exist on exchange
        for key, sl_id in list(self._sl_order_ids.items()):
            pos = self._positions.get(key)
            if not pos or pos.condition == 0.0:
                continue
            symbol = pos.symbol
            tf_label = self._tf_labels.get(key, "")
            try:
                order = self._client.get_order(symbol, sl_id)
                status = order.get("status", "")
                if status in ("CANCELED", "EXPIRED", "FILLED"):
                    if status == "FILLED":
                        msg = f"[SYNC] {symbol} [{tf_label}]: SL order {sl_id} already FILLED"
                        logger.info(msg)
                        warnings.append(msg)
                    else:
                        msg = (
                            f"[SYNC] {symbol} [{tf_label}]: SL order {sl_id} is {status} — "
                            f"re-placing SL for protection"
                        )
                        logger.warning(msg)
                        warnings.append(msg)
                        qty = self._position_qty.get(key, 0)
                        if qty > 0:
                            self._place_sl_order(symbol, pos, qty)
            except Exception as e:
                logger.error("[SYNC] Failed to check SL order %s for %s: %s", sl_id, key, e)

        if warnings:
            self.sync_warnings.extend(warnings)
            # Keep last 50 warnings
            self.sync_warnings = self.sync_warnings[-50:]

        # Also refresh balance during sync
        try:
            self.refresh_balance()
        except Exception:
            pass

        return warnings

    # ------------------------------------------------------------------
    # Signal Processing (same logic as Simulator)
    # ------------------------------------------------------------------

    def process_signal(self, signal: Signal, entry_time: int = 0) -> list[LiveTrade]:
        """Handle a new entry signal — places real orders on Binance.

        If pair is OBSERVING, this is the first signal → activate pair.
        Each TF manages positions independently.
        """
        closed_trades: list[LiveTrade] = []
        symbol = signal.symbol
        tf_label = signal.tf_label or ""
        key = self._pos_key(symbol, tf_label)
        size_mult = signal.size_multiplier if signal.size_multiplier > 0 else 1.0

        pc = self._pair_configs.get(symbol)
        if not pc:
            logger.warning("[SIGNAL] No config for %s — skipping", symbol)
            return closed_trades

        # Prevent reopening from same signal after TP/SL exit
        last_ts = self._last_signal_ts.get(key, 0)
        if signal.timestamp == last_ts and not self.has_position(symbol, tf_label):
            return closed_trades

        # If OBSERVING → activate on first signal
        if self.is_observing(symbol):
            self.activate_pair(symbol)

        if self.has_position(symbol, tf_label):
            existing = self._positions[key]
            if existing.side == signal.side:
                return closed_trades  # same direction — no pyramiding
            # Opposite direction — close existing (reversal within same TF)
            closed_trades.extend(self._close_position(key, signal.price))

        # ── Account protection checks ──
        self.refresh_balance()
        block_reason = self._check_account_protection(symbol)
        if block_reason:
            logger.warning("[SIGNAL] Blocked for %s [%s]: %s", symbol, tf_label, block_reason)
            return closed_trades

        # Margin: dynamic compounding or fixed
        if self._dyncomp_enabled and self._dyncomp_tiers:
            comp_pct = get_dynamic_comp_pct(self.balance, self._dyncomp_tiers)
            margin = calc_step_margin(self.balance, comp_pct) * size_mult
            logger.info(
                "[DYNCOMP] balance=%.2f comp_pct=%.1f%% step_margin=%.2f",
                self.balance, comp_pct, margin,
            )
        else:
            margin = pc.margin * size_mult
        if self.available_balance < margin:
            logger.warning(
                "[SIGNAL] Insufficient balance for %s [%s]: need %.2f, have %.2f",
                symbol, tf_label, margin, self.available_balance,
            )
            return closed_trades

        # Calculate quantity with adjusted margin
        qty = self._client.calc_quantity(symbol, margin, pc.leverage, signal.price)
        if qty <= 0:
            logger.warning("[SIGNAL] Calculated qty=0 for %s [%s] — skipping", symbol, tf_label)
            return closed_trades

        # Place market order + verify fill
        order_side = "BUY" if signal.side == "LONG" else "SELL"
        try:
            result = self._client.market_order(symbol, order_side, qty)
            fill_price = self._verify_fill(symbol, result, signal.price)
        except Exception as e:
            logger.error("[ORDER] Market order failed for %s [%s]: %s", symbol, tf_label, e)
            return closed_trades

        # Create position state with per-TF risk manager
        risk_mgr = self._get_risk_mgr(tf_label)
        pos = risk_mgr.open_position(
            symbol, signal.side, fill_price, signal.atr_value,
            margin_per_trade=margin, leverage=pc.leverage,
        )
        pos.entry_time = entry_time or signal.timestamp
        self._positions[key] = pos
        self._position_qty[key] = qty
        self._size_multipliers[key] = size_mult
        self._tf_labels[key] = tf_label
        self._last_signal_ts[key] = signal.timestamp

        # Entry fee estimation (market order = taker)
        notional = qty * fill_price
        entry_fee = notional * self._taker_fee
        self.total_fees += entry_fee

        logger.info(
            "[ENTRY] %s %s [%s] @ %.4f qty=%.6f notional=%.2f (x%.0f)",
            symbol, signal.side, tf_label, fill_price, qty, notional, size_mult,
        )

        # Place DCA + TP LIMIT orders on exchange
        self._place_dca_orders(key, pos)
        self._place_tp_order(key, pos)

        return closed_trades

    def _close_position(self, key: str, exit_price: float) -> list[LiveTrade]:
        """Force-close a position (reversal). Sends market close order.

        key is "symbol:tf_label" format.
        """
        if key not in self._positions or self._positions[key].condition == 0.0:
            return []

        pos = self._positions[key]
        symbol = pos.symbol
        tf_label = self._tf_labels.get(key, "")
        qty = self._position_qty.get(key, 0)
        if qty <= 0:
            return []

        # KILL SWITCH: Cancel ALL grid orders BEFORE closing position
        self._cancel_all_grid_orders(key, pos)

        # Place market close order + verify fill
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        actual_qty = qty * pos.remaining_qty
        try:
            result = self._client.market_order(symbol, close_side, actual_qty, reduce_only=True)
            fill_price = self._verify_fill(symbol, result, exit_price)
        except Exception as e:
            logger.error("[CLOSE] Market close failed for %s [%s]: %s", symbol, tf_label, e)
            fill_price = exit_price

        # Record trade
        self._trade_counter += 1
        size_mult = self._size_multipliers.get(key, 1.0)
        pc = self._pair_configs.get(symbol)
        notional = (pc.margin * size_mult * pc.leverage * pos.remaining_qty) if pc else actual_qty * fill_price

        if pos.side == "LONG":
            pnl_pct = (fill_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl_pct = (pos.entry_price - fill_price) / pos.entry_price * 100

        pnl_usdt = notional * pnl_pct / 100
        exit_fee = actual_qty * fill_price * self._taker_fee  # market close = taker

        self._update_stats(pnl_usdt, exit_fee)

        trade = LiveTrade(
            id=self._trade_counter,
            symbol=symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=fill_price,
            exit_time=int(time.time() * 1000),
            exit_reason="REVERSAL",
            qty=actual_qty,
            qty_usdt=round(notional, 2),
            leverage=pc.leverage if pc else 1,
            pnl_usdt=round(pnl_usdt, 4),
            pnl_percent=round(pnl_pct, 4),
            fee_usdt=round(exit_fee, 4),
        )
        self.trades.append(trade)

        # Mark closed
        pos.condition = 0.0
        pos.remaining_qty = 0.0
        self._position_qty.pop(key, None)
        self._sl_order_ids.pop(key, None)

        logger.info(
            "[CLOSE] %s %s [%s] @ %.4f | PnL=%.4f USDT (%.2f%%)",
            symbol, pos.side, tf_label, fill_price, pnl_usdt, pnl_pct,
        )

        return [trade]

    def _place_dca_orders(self, key: str, pos: PositionState) -> None:
        """Place DCA LIMIT order at pending KC level (if any).

        In the KC-based approach, DCA price is set by pending_dca_price
        which gets updated each candle from Keltner Channel bands.
        """
        symbol = pos.symbol
        pc = self._pair_configs.get(symbol)
        if not pc or pos.pending_dca_price <= 0:
            return

        risk_mgr = self._get_risk_mgr(self._tf_labels.get(key, ""))
        if pos.dca_fills_count >= risk_mgr._max_dca_steps:
            return  # max DCA reached

        if pos.pending_dca_order_id > 0:
            return  # already placed

        # Use dynamic comp for DCA margin
        if self._dyncomp_enabled and self._dyncomp_tiers:
            comp_pct = get_dynamic_comp_pct(self.balance, self._dyncomp_tiers)
            dca_margin = calc_step_margin(self.balance, comp_pct)
        else:
            dca_margin = pos.margin_per_step

        order_side = "BUY" if pos.side == "LONG" else "SELL"
        qty = self._client.calc_quantity(symbol, dca_margin, pos.leverage, pos.pending_dca_price)
        if qty <= 0:
            return
        try:
            price = self._client.calc_price(symbol, pos.pending_dca_price)
            result = self._client.limit_order(symbol, order_side, qty, price)
            pos.pending_dca_order_id = result.get("orderId", 0)
            logger.info("[DCA_PLACED] %s %s qty=%.6f @ %.4f orderId=%s",
                        symbol, order_side, qty, price, pos.pending_dca_order_id)
        except Exception as e:
            logger.error("[DCA_ORDER] Failed for %s: %s", symbol, e)

    def _place_tp_order(self, key: str, pos: PositionState) -> None:
        """Place TP LIMIT order on exchange."""
        tp_price = pos.pending_tp_price or pos.tp_price
        if tp_price <= 0 or pos.condition == 0.0:
            return

        symbol = pos.symbol
        total_qty = self._position_qty.get(key, 0)
        if total_qty <= 0:
            return

        # Use tp_close_pct from risk manager config (default 5%)
        risk_mgr = self._get_risk_mgr(self._tf_labels.get(key, ""))
        tp_pct = risk_mgr._tp_close_pct
        tp_qty = total_qty * pos.remaining_qty * tp_pct
        if tp_qty <= 0:
            return

        close_side = "SELL" if pos.side == "LONG" else "BUY"
        try:
            price = self._client.calc_price(symbol, tp_price)
            result = self._client.limit_order(symbol, close_side, tp_qty, price, reduce_only=True)
            pos.tp_order_id = result.get("orderId", 0)
            logger.info("[TP_PLACED] %s %s qty=%.6f @ %.4f orderId=%s (%.0f%%)",
                        symbol, close_side, tp_qty, price, pos.tp_order_id, tp_pct * 100)
        except Exception as e:
            logger.error("[TP_ORDER] Failed for %s: %s", symbol, e)

    def _place_sl_order(self, symbol: str, pos: PositionState, qty: float) -> None:
        """Place hard stop as STOP_MARKET order on exchange."""
        if pos.hard_stop_price <= 0 or pos.condition == 0.0:
            return

        close_side = "SELL" if pos.side == "LONG" else "BUY"
        key = None
        for k, p in self._positions.items():
            if p is pos:
                key = k
                break

        try:
            result = self._client.stop_market_order(
                symbol, close_side, qty, pos.hard_stop_price,
            )
            order_id = result.get("orderId", 0)
            if key:
                self._sl_order_ids[key] = order_id
            logger.info(
                "[SL_PLACED] %s %s qty=%.6f stop=%.4f orderId=%s",
                symbol, close_side, qty, pos.hard_stop_price, order_id,
            )
        except Exception as e:
            logger.error("[SL_ORDER] Failed for %s: %s", symbol, e)

    def _cancel_all_grid_orders(self, key: str, pos: PositionState) -> None:
        """Cancel ALL open DCA + TP limit orders for this position (kill switch cleanup)."""
        symbol = pos.symbol

        # Cancel DCA orders
        for dca in pos.dca_levels:
            if dca.order_id > 0 and not dca.filled:
                try:
                    self._client.cancel_order(symbol, dca.order_id)
                    logger.info("[CANCEL_DCA] %s L%d orderId=%d", symbol, dca.step, dca.order_id)
                except Exception:
                    pass
                dca.order_id = 0

        # Cancel TP order
        if pos.tp_order_id > 0:
            try:
                self._client.cancel_order(symbol, pos.tp_order_id)
                logger.info("[CANCEL_TP] %s orderId=%d", symbol, pos.tp_order_id)
            except Exception:
                pass
            pos.tp_order_id = 0

    def process_candle(self, symbol: str, high: float, low: float, close_time: int,
                       tf_label: str = "", candle_close: float = 0.0,
                       current_atr: float = 0.0) -> list[LiveTrade]:
        """Check DCA/TP fills for active positions.

        In live mode, orders are on-exchange as LIMIT. This method checks
        fill status and updates state accordingly.
        For now, uses candle-based simulation (same as dry-run) until
        WebSocket user data stream is implemented for real-time fill detection.
        """
        keys_to_check = []
        if tf_label:
            keys_to_check.append(self._pos_key(symbol, tf_label))
        else:
            for key in list(self._positions.keys()):
                if key.startswith(symbol + ":") or key == symbol:
                    keys_to_check.append(key)

        completed: list[LiveTrade] = []
        for key in keys_to_check:
            if key not in self._positions or self._positions[key].condition == 0.0:
                continue

            pos = self._positions[key]
            risk_mgr = self._get_risk_mgr(self._tf_labels.get(key, ""))

            # 0a. Dynamic SL check (close-based, tighter) — uses current ATR, not entry ATR
            atr_for_sl = current_atr if current_atr > 0 else pos.entry_atr
            if candle_close > 0 and atr_for_sl > 0:
                dyn_hit, dyn_price = risk_mgr.check_dynamic_sl(
                    pos, candle_close, atr_for_sl,
                )
                if dyn_hit:
                    logger.warning(
                        "[DYN_SL] %s %s hit @ %.4f (close=%.4f)",
                        pos.symbol, pos.side, dyn_price, candle_close,
                    )
                    trades = self._close_position(key, dyn_price)
                    for t in trades:
                        t.exit_reason = "DYN_SL"
                    completed.extend(trades)
                    continue

            # 0a2. Percentage hard stop check (after DCA full)
            if self._pct_stop_enabled and candle_close > 0:
                pct_hit, pct_price = risk_mgr.check_pct_hard_stop(
                    pos, candle_close,
                )
                if pct_hit:
                    logger.warning(
                        "[PCT_STOP] %s %s hit @ %.4f (close=%.4f)",
                        pos.symbol, pos.side, pct_price, candle_close,
                    )
                    trades = self._close_position(key, pct_price)
                    for t in trades:
                        t.exit_reason = "PCT_STOP"
                    completed.extend(trades)
                    continue

            # 0b. Hard stop check (emergency backup, H/L based)
            stop_hit, stop_price, stop_reason = risk_mgr.check_hard_stop(pos, high, low)
            if stop_hit:
                logger.warning(
                    "[%s] %s %s hit @ %.4f", stop_reason, pos.symbol, pos.side, stop_price,
                )
                trades = self._close_position(key, stop_price)
                for t in trades:
                    t.exit_reason = stop_reason
                completed.extend(trades)
                continue

            # 1. Check Keltner DCA/TP signals using candle H/L vs pending prices
            # DCA check: candle touches pending DCA level
            dca_filled = False
            if pos.dca_fills_count < risk_mgr._max_dca_steps and pos.pending_dca_price > 0:
                dca_hit = False
                if pos.side == "LONG" and low <= pos.pending_dca_price:
                    dca_hit = True
                elif pos.side == "SHORT" and high >= pos.pending_dca_price:
                    dca_hit = True

                if dca_hit:
                    # Use dynamic comp for DCA margin
                    if self._dyncomp_enabled and self._dyncomp_tiers:
                        comp_pct = get_dynamic_comp_pct(self.balance, self._dyncomp_tiers)
                        dca_margin = calc_step_margin(self.balance, comp_pct)
                    else:
                        dca_margin = pos.margin_per_step
                    pos.margin_per_step = dca_margin

                    risk_mgr.process_dca_fill(pos, pos.pending_dca_price)
                    # Recalculate hard stop after DCA
                    if pos.entry_atr > 0:
                        risk_mgr.update_hard_stop(pos, pos.entry_atr)
                    entry_fee = dca_margin * pos.leverage * self._maker_fee
                    self.total_fees += entry_fee
                    dca_filled = True

            # 2. TP check: candle touches pending TP level (only if DCA filled)
            if not dca_filled and pos.dca_fills_count > 0 and pos.pending_tp_price > 0:
                tp_hit = False
                if pos.side == "LONG" and high >= pos.pending_tp_price:
                    tp_hit = True
                elif pos.side == "SHORT" and low <= pos.pending_tp_price:
                    tp_hit = True

                if tp_hit:
                    tp_fill_price = pos.pending_tp_price
                    avg_before = pos.average_entry_price
                    closed_notional = risk_mgr.process_tp_fill(pos, tp_fill_price)
                    if closed_notional > 0:
                        self._trade_counter += 1

                        if pos.side == "LONG":
                            pnl_pct = (tp_fill_price - avg_before) / avg_before * 100
                        else:
                            pnl_pct = (avg_before - tp_fill_price) / avg_before * 100

                        pnl_usdt = closed_notional * pnl_pct / 100
                        tp_fee = closed_notional * self._maker_fee

                        self._update_stats(pnl_usdt, tp_fee)

                        trade = LiveTrade(
                            id=self._trade_counter,
                            symbol=pos.symbol,
                            side=pos.side,
                            entry_price=avg_before,
                            entry_time=pos.entry_time,
                            exit_price=tp_fill_price,
                            exit_time=close_time,
                            exit_reason="TP",
                            qty=0,
                            qty_usdt=round(closed_notional, 2),
                            leverage=pos.leverage,
                            pnl_usdt=round(pnl_usdt, 4),
                            pnl_percent=round(pnl_pct, 4),
                            fee_usdt=round(tp_fee, 4),
                        )
                        completed.append(trade)
                        self.trades.append(trade)

        return completed
    def _update_stats(self, pnl_usdt: float, fee: float) -> None:
        self.total_pnl += pnl_usdt
        self.total_fees += fee
        self.total_trades += 1
        if pnl_usdt > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

    # ------------------------------------------------------------------
    # Status / Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics (same shape as Simulator.get_stats)."""
        win_rate = (
            self.winning_trades / self.total_trades * 100
            if self.total_trades > 0
            else 0
        )
        drawdown_pct = 0.0
        if self._initial_balance > 0:
            drawdown_pct = (self._initial_balance - self.balance) / self._initial_balance * 100

        open_count = sum(1 for pos in self._positions.values() if pos.condition != 0.0)

        return {
            "initial_balance": round(self._initial_balance, 2),
            "current_balance": round(self.balance, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_pnl_pct": 0,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(win_rate, 2),
            "total_fees": round(self.total_fees, 4),
            "maker_fees": 0,
            "taker_fees": 0,
            "leverage": 0,
            # Protection stats
            "drawdown_pct": round(drawdown_pct, 2),
            "max_drawdown_limit": self._max_drawdown_pct,
            "open_positions": open_count,
            "max_open_positions": self._max_open_positions,
            "circuit_breaker": self.circuit_breaker_triggered,
            "circuit_breaker_reason": self.circuit_breaker_reason,
            "sync_warnings": self.sync_warnings[-10:],
        }

    def get_pair_state(self, symbol: str) -> str:
        """Return pair's current state (OBSERVING/ACTIVE)."""
        return self._pair_states.get(symbol, PairState.OBSERVING).value

    def emergency_close_all(self) -> list[LiveTrade]:
        """Emergency: close all positions immediately.

        Fetches real-time prices from Binance book ticker for accurate PnL.
        Falls back to entry price only if price fetch fails.
        """
        all_trades: list[LiveTrade] = []

        # Try to get current prices for all symbols at once
        current_prices: dict[str, float] = {}
        for key, pos in list(self._positions.items()):
            if pos.condition == 0.0:
                continue
            symbol = pos.symbol
            if symbol not in current_prices:
                try:
                    ticker = self._client._request(
                        "GET", "/fapi/v1/ticker/price",
                        {"symbol": symbol}, signed=False,
                    )
                    current_prices[symbol] = float(ticker.get("price", 0))
                except Exception:
                    pass

        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition != 0.0:
                price = current_prices.get(pos.symbol) or pos.entry_price
                trades = self._close_position(key, price)
                all_trades.extend(trades)
        return all_trades
