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

        # ── WS Fill Detection (idempotency + order tracking) ──
        # Processed order IDs — idempotency guard (bounded to last 2000)
        self._processed_order_ids: set[int] = set()
        # Order ID → (pos_key, order_type) mapping
        # Every placed order is registered here. WS fill events match by this map.
        # Old IDs stay after cancel+replace → late WS events still match.
        self._order_id_map: dict[int, tuple[str, str]] = {}
        # WS connection health (for watchdog)
        self._ws_connected: bool = False
        self._ws_last_event_ts: float = 0.0
        # Fill event log (for debug endpoint, last 50)
        self._fill_log: list[dict] = []

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

        # Margin type: borsadaki mevcut ayari koru (CROSS/ISOLATED degistirme)
        # Stop korumalari kapali oldugu icin CROSS daha guvenli (likidasyon daha uzak)

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

    def recover_from_exchange(self, tf_label: str = "3m") -> list[str]:
        """Recover position state from exchange after bot restart.

        Reads open positions and pending orders from Binance,
        reconstructs PositionState, and populates _order_id_map.
        Returns list of recovery messages.
        """
        msgs: list[str] = []
        try:
            positions = self._client.get_positions()
        except Exception as e:
            msgs.append(f"[RECOVERY] Failed to fetch positions: {e}")
            return msgs

        for ep in positions:
            symbol = ep["symbol"]
            side = ep["side"]
            amount = ep["amount"]
            entry_price = ep["entry_price"]
            leverage = ep["leverage"]

            key = self._pos_key(symbol, tf_label)
            if key in self._positions and self._positions[key].condition != 0.0:
                # Already tracked — just sync qty
                self._position_qty[key] = amount
                continue

            # Reconstruct PositionState
            pc = self._pair_configs.get(symbol)
            if not pc:
                msgs.append(f"[RECOVERY] {symbol}: no pair config, skipping")
                continue

            risk_mgr = self._get_risk_mgr(tf_label)
            pos = risk_mgr.open_position(
                symbol, side, entry_price, 0.0,
                margin_per_trade=pc.margin, leverage=leverage,
            )
            pos.entry_time = int(time.time() * 1000)
            self._positions[key] = pos
            self._position_qty[key] = amount
            self._tf_labels[key] = tf_label
            self._pair_states[symbol] = PairState.ACTIVE

            msgs.append(f"[RECOVERY] {symbol} {side} reconstructed: qty={amount} entry={entry_price}")
            logger.info("[RECOVERY] %s %s reconstructed: qty=%.6f entry=%.4f",
                        symbol, side, amount, entry_price)

        # Load pending orders into _order_id_map
        for symbol in list(self._pair_configs.keys()):
            try:
                open_orders = self._client.get_open_orders(symbol)
                for o in open_orders:
                    oid = o.get("orderId", 0)
                    if oid <= 0:
                        continue
                    key = self._pos_key(symbol, tf_label)
                    reduce_only = o.get("reduceOnly", False)
                    order_type_str = o.get("type", "")

                    if order_type_str == "STOP_MARKET":
                        self._order_id_map[oid] = (key, "SL")
                        self._sl_order_ids[key] = oid
                    elif reduce_only:
                        self._order_id_map[oid] = (key, "TP")
                        pos = self._positions.get(key)
                        if pos:
                            pos.tp_order_id = oid
                            pos.pending_tp_price = float(o.get("price", 0))
                    else:
                        self._order_id_map[oid] = (key, "DCA")
                        pos = self._positions.get(key)
                        if pos:
                            pos.pending_dca_order_id = oid
                            pos.pending_dca_price = float(o.get("price", 0))

                    msgs.append(f"[RECOVERY] {symbol}: order #{oid} mapped as {self._order_id_map[oid][1]}")
            except Exception as e:
                msgs.append(f"[RECOVERY] Failed to load orders for {symbol}: {e}")

        return msgs

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

        # Check bot positions against exchange + sync _position_qty
        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition == 0.0:
                continue

            symbol = pos.symbol
            tf_label = self._tf_labels.get(key, "")

            # Sync _position_qty with exchange qty
            if symbol in exchange_map:
                exchange_qty = exchange_map[symbol]["amount"]
                tracked_qty = self._position_qty.get(key, 0)
                if abs(exchange_qty - tracked_qty) > 0.001 * max(exchange_qty, 1):
                    logger.info(
                        "[SYNC_QTY] %s [%s]: tracked=%.6f exchange=%.6f — correcting",
                        symbol, tf_label, tracked_qty, exchange_qty,
                    )
                    self._position_qty[key] = exchange_qty

            if symbol not in exchange_map:
                msg = (
                    f"[SYNC] {symbol} {pos.side} [{tf_label}]: bot has open position but "
                    f"exchange shows no position — marking as closed"
                )
                logger.warning(msg)
                warnings.append(msg)

                # Cancel ALL orphan orders for this symbol (SL, DCA, TP)
                old_sl = self._sl_order_ids.get(key)
                if old_sl:
                    try:
                        self._client.cancel_order(symbol, old_sl)
                    except Exception:
                        pass
                if pos.pending_dca_order_id > 0:
                    try:
                        self._client.cancel_order(symbol, pos.pending_dca_order_id)
                        logger.info("[SYNC_CLEANUP] Cancelled orphan DCA order %d for %s", pos.pending_dca_order_id, symbol)
                    except Exception:
                        pass
                    pos.pending_dca_order_id = 0
                if pos.tp_order_id > 0:
                    try:
                        self._client.cancel_order(symbol, pos.tp_order_id)
                        logger.info("[SYNC_CLEANUP] Cancelled orphan TP order %d for %s", pos.tp_order_id, symbol)
                    except Exception:
                        pass
                    pos.tp_order_id = 0

                pos.condition = 0.0
                pos.remaining_qty = 0.0
                self._position_qty.pop(key, None)
                self._sl_order_ids.pop(key, None)

        # Check 5: Verify pending DCA/TP orders still exist on exchange
        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition == 0.0:
                continue
            symbol = pos.symbol
            # Verify DCA order
            if pos.pending_dca_order_id > 0:
                try:
                    order = self._client.get_order(symbol, pos.pending_dca_order_id)
                    status = order.get("status", "")
                    if status in ("CANCELED", "EXPIRED", "REJECTED"):
                        msg = f"[SYNC] {symbol}: DCA order {pos.pending_dca_order_id} is {status} — clearing"
                        logger.warning(msg)
                        warnings.append(msg)
                        pos.pending_dca_order_id = 0
                except Exception:
                    pass
            # Verify TP order
            if getattr(pos, 'tp_order_id', 0) > 0:
                try:
                    order = self._client.get_order(symbol, pos.tp_order_id)
                    status = order.get("status", "")
                    if status in ("CANCELED", "EXPIRED", "REJECTED"):
                        msg = f"[SYNC] {symbol}: TP order {pos.tp_order_id} is {status} — clearing"
                        logger.warning(msg)
                        warnings.append(msg)
                        pos.tp_order_id = 0
                except Exception:
                    pass

        # Check 6: Verify SL orders still exist on exchange
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

        # ── WS Health Check ──
        if self._ws_connected and time.time() - self._ws_last_event_ts > 300:
            msg = "[WATCHDOG] WS silent for 5min — triggering catch-up poll"
            logger.warning(msg)
            warnings.append(msg)
            self._ws_connected = False
            self._catchup_poll_all_orders()

        # ── Dust position cleanup ──
        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition == 0.0:
                continue
            qty = self._position_qty.get(key, 0)
            if qty > 0 and qty * (pos.average_entry_price or 1) < 10.0:  # < $10 notional = dust
                logger.info("[SYNC_DUST] %s qty=%.6f — closing dust position", pos.symbol, qty)
                # Cancel orders FIRST (before marking closed, so no new fills sneak in)
                self._cancel_all_grid_orders(key, pos)
                pos.condition = 0.0
                pos.remaining_qty = 0.0
                pos.pending_dca_order_id = 0
                pos.tp_order_id = 0
                self._position_qty[key] = 0
                warnings.append(f"[SYNC] {pos.symbol}: dust position closed (qty={qty})")

        # ── Invariant check on all open positions ──
        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition == 0.0:
                continue
            tf_label = self._tf_labels.get(key, "")
            risk_mgr = self._get_risk_mgr(tf_label)
            if pos.dca_fills_count > risk_mgr._max_dca_steps:
                msg = f"[WATCHDOG] {pos.symbol}: dca_count={pos.dca_fills_count} > max={risk_mgr._max_dca_steps}!"
                logger.critical(msg)
                warnings.append(msg)
                self.circuit_breaker_triggered = True
                self.circuit_breaker_reason = msg

        # ── Bound _order_id_map (prevent memory leak) ──
        if len(self._order_id_map) > 5000:
            # Keep only recent entries
            recent = dict(list(self._order_id_map.items())[-2000:])
            self._order_id_map = recent

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
            # Verify close succeeded — if position still open, don't open opposite
            if self.has_position(symbol, tf_label):
                logger.error("[REVERSAL] Failed to close %s %s — aborting new entry", symbol, existing.side)
                return closed_trades

        # ── Account protection checks (balance should be pre-refreshed by caller) ──
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
            # Register IMMEDIATELY — before verify, so WS event doesn't race
            entry_order_id = result.get("orderId", 0)
            if entry_order_id > 0:
                self._order_id_map[entry_order_id] = (key, "ENTRY")
                self._processed_order_ids.add(entry_order_id)
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

        # Backtest parity: entry candle'da DCA/TP konulmuyor.
        # pending_dca_price=0, pending_tp_price=0 — sonraki candle KC update yapacak.

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

        # Place market close order + verify fill — use real exchange qty
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        # Get actual qty from exchange to avoid mismatch after DCA
        try:
            exchange_positions = self._client.get_positions()
            for ep in exchange_positions:
                if ep["symbol"] == symbol:
                    actual_qty = ep["amount"]
                    break
            else:
                actual_qty = qty * pos.remaining_qty
        except Exception:
            actual_qty = qty * pos.remaining_qty
        try:
            result = self._client.market_order(symbol, close_side, actual_qty, reduce_only=True)
            fill_price = self._verify_fill(symbol, result, exit_price)
        except Exception as e:
            logger.error("[CLOSE] Market close failed for %s [%s]: %s — position NOT closed", symbol, tf_label, e)
            return []  # DON'T mark as closed — let sync detect

        # Record trade
        self._trade_counter += 1
        size_mult = self._size_multipliers.get(key, 1.0)
        pc = self._pair_configs.get(symbol)
        notional = actual_qty * pos.average_entry_price if actual_qty > 0 else (pc.margin * size_mult * pc.leverage * pos.remaining_qty if pc else 0)

        avg_entry = pos.average_entry_price if pos.average_entry_price > 0 else (pos.entry_price if pos.entry_price > 0 else fill_price)
        if pos.side == "LONG":
            pnl_pct = (fill_price - avg_entry) / avg_entry * 100 if avg_entry > 0 else 0
        else:
            pnl_pct = (avg_entry - fill_price) / avg_entry * 100 if avg_entry > 0 else 0

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
        """Place DCA LIMIT order at pending KC level.

        Fill detection is handled by WS User Data Stream (process_order_fill).
        This method only: cancel old → place new → handle immediate fill.
        """
        symbol = pos.symbol
        pc = self._pair_configs.get(symbol)
        if not pc or pos.pending_dca_price <= 0:
            return

        # Don't place DCA on dust positions
        current_qty = self._position_qty.get(key, 0)
        if current_qty > 0 and current_qty * pos.pending_dca_price < 10.0:
            return

        risk_mgr = self._get_risk_mgr(self._tf_labels.get(key, ""))
        if pos.dca_fills_count >= risk_mgr._max_dca_steps:
            return  # max DCA reached

        # Cancel existing DCA order — verify cancel succeeded
        if pos.pending_dca_order_id > 0:
            old_id = pos.pending_dca_order_id
            try:
                cancel_result = self._client.cancel_order(symbol, old_id)
                # Cancel succeeded — safe to clear
                pos.pending_dca_order_id = 0
            except Exception:
                # Cancel failed — order may have filled or network error
                # Check status before clearing
                try:
                    order = self._client.get_order(symbol, old_id)
                    status = order.get("status", "")
                    if status == "FILLED":
                        # Order filled before cancel — process directly (no recursion)
                        fill_p = float(order.get("avgPrice", 0))
                        fill_q = float(order.get("executedQty", 0))
                        if old_id not in self._processed_order_ids:
                            self._processed_order_ids.add(old_id)
                            self._process_dca_fill_live(key, pos, fill_p, fill_q, old_id,
                                                         place_next_orders=False)
                    pos.pending_dca_order_id = 0
                except Exception:
                    pos.pending_dca_order_id = 0  # Clear anyway, WS will catch if filled

        # Calculate DCA margin with graduated multiplier
        dca_mult = risk_mgr.get_dca_multiplier(pos.dca_fills_count)
        if self._dyncomp_enabled and self._dyncomp_tiers:
            comp_pct = get_dynamic_comp_pct(self.balance, self._dyncomp_tiers)
            dca_margin = calc_step_margin(self.balance, comp_pct) * dca_mult
        else:
            dca_margin = pos.margin_per_step * dca_mult

        order_side = "BUY" if pos.side == "LONG" else "SELL"
        qty = self._client.calc_quantity(symbol, dca_margin, pos.leverage, pos.pending_dca_price)
        if qty <= 0:
            return
        try:
            price = self._client.calc_price(symbol, pos.pending_dca_price)
            import math
            info = self._client.get_symbol_info(symbol)
            step_size = 0.001
            for f in info.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step_size = float(f.get("stepSize", 0.001))
                    break
            qty = math.floor(qty / step_size) * step_size
            if qty <= 0:
                return

            result = self._client.limit_order(symbol, order_side, qty, price)
            order_id = result.get("orderId", 0)
            order_status = result.get("status", "")

            # Register in order_id_map (WS fill events match from this map)
            if order_id > 0:
                self._order_id_map[order_id] = (key, "DCA")

            logger.info("[DCA_PLACED] %s %s qty=%.6f @ %.6f mult=%.2f orderId=%s status=%s",
                        symbol, order_side, qty, price, dca_mult, order_id, order_status)

            # Handle IMMEDIATE fill — max 1 DCA per cycle (matches backtest behavior)
            # Backtest: each candle max 1 DCA (if block, not while loop)
            # Next DCA will be placed on next candle's KC update
            if order_status == "FILLED":
                self._processed_order_ids.add(order_id)
                fill_price = float(result.get("avgPrice", price))
                fill_qty = float(result.get("executedQty", qty))
                self._process_dca_fill_live(key, pos, fill_price, fill_qty, order_id,
                                             place_next_orders=False)
                # Update TP for new position size
                self._place_tp_order(key, pos)
                # Do NOT place next DCA here — wait for next candle (backtest parity)
                pos.pending_dca_order_id = 0
            else:
                pos.pending_dca_order_id = order_id
        except Exception as e:
            logger.error("[DCA_ORDER] Failed for %s: qty=%.6f price=%.6f err=%s",
                         symbol, qty, pos.pending_dca_price, e)
            pos.pending_dca_order_id = 0

    def _place_tp_order(self, key: str, pos: PositionState) -> None:
        """Place TP LIMIT order on exchange.

        Fill detection is handled by WS User Data Stream (process_order_fill).
        This method only: cancel old → place new → handle immediate fill.

        TP only placed when dca_fills_count > 0 (matches backtest behavior).
        At dca_count=0, position waits for DCA fill before TP is set.
        """
        symbol = pos.symbol
        tp_price = pos.pending_tp_price or pos.tp_price
        if tp_price <= 0 or pos.condition == 0.0:
            return

        # No TP at dca_count=0 — backtest behavior: TP only after DCA fill
        if pos.dca_fills_count <= 0:
            return

        # Cancel existing TP order — verify cancel succeeded
        if pos.tp_order_id > 0:
            old_id = pos.tp_order_id
            try:
                self._client.cancel_order(symbol, old_id)
                pos.tp_order_id = 0
            except Exception:
                try:
                    order = self._client.get_order(symbol, old_id)
                    status = order.get("status", "")
                    if status == "FILLED":
                        fill_p = float(order.get("avgPrice", 0))
                        fill_q = float(order.get("executedQty", 0))
                        if old_id not in self._processed_order_ids:
                            self._processed_order_ids.add(old_id)
                            self._process_tp_fill_live(key, pos, fill_p, fill_q, old_id,
                                                        place_next_orders=False)
                    pos.tp_order_id = 0
                except Exception:
                    pos.tp_order_id = 0

        # Use _position_qty (synced from exchange after every fill)
        # Don't re-query exchange here — sync may be more recent than a fresh query
        # (Binance position update can lag behind order fill by a few hundred ms)
        actual_qty = self._position_qty.get(key, 0)
        if actual_qty <= 0:
            logger.warning("[TP_ORDER] %s: no position qty found (internal=%s, exchange=0)",
                          symbol, self._position_qty.get(key, 0))
            return

        # Use graduated tp_close_pct based on DCA fills
        risk_mgr = self._get_risk_mgr(self._tf_labels.get(key, ""))
        tp_pct = risk_mgr.get_tp_close_pct(pos.dca_fills_count)
        tp_qty = actual_qty * tp_pct
        if tp_qty <= 0:
            return

        # Round qty to symbol's stepSize
        import math
        info = self._client.get_symbol_info(symbol)
        step_size = 1.0
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step_size = float(f.get("stepSize", 1))
                break
        if step_size > 0:
            tp_qty = math.floor(tp_qty / step_size) * step_size
        else:
            qty_precision = info.get("quantityPrecision", 0)
            tp_qty = round(tp_qty, qty_precision)
        if tp_qty <= 0:
            return

        close_side = "SELL" if pos.side == "LONG" else "BUY"
        try:
            price = self._client.calc_price(symbol, tp_price)
            result = self._client.limit_order(symbol, close_side, tp_qty, price, reduce_only=True)
            order_id = result.get("orderId", 0)
            order_status = result.get("status", "")

            # Register in order_id_map (WS fill events match from this map)
            if order_id > 0:
                self._order_id_map[order_id] = (key, "TP")

            logger.info("[TP_PLACED] %s %s qty=%.6f @ %.4f orderId=%s (%.0f%%) exchange_qty=%.6f status=%s",
                        symbol, close_side, tp_qty, price, order_id, tp_pct * 100, actual_qty, order_status)

            # Do NOT handle immediate fill here — backtest parity
            # If TP fills instantly (marketable limit), WS event will detect it
            # on the next cycle. This matches backtest elif behavior:
            # DCA candle does not process TP, even if price touched KC upper.
            pos.tp_order_id = order_id
        except Exception as e:
            logger.error("[TP_ORDER] Failed for %s: qty=%.6f price=%.4f err=%s", symbol, tp_qty, tp_price, e)

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
                # Register in order_id_map for WS fill detection
                if order_id > 0:
                    self._order_id_map[order_id] = (key, "SL")
            logger.info(
                "[SL_PLACED] %s %s qty=%.6f stop=%.4f orderId=%s",
                symbol, close_side, qty, pos.hard_stop_price, order_id,
            )
        except Exception as e:
            logger.error("[SL_ORDER] Failed for %s: %s", symbol, e)

    def _cancel_all_grid_orders(self, key: str, pos: PositionState) -> None:
        """Cancel ALL open DCA + TP limit orders for this position (kill switch cleanup)."""
        symbol = pos.symbol

        # Cancel pending DCA order (KC-based approach)
        if pos.pending_dca_order_id > 0:
            try:
                self._client.cancel_order(symbol, pos.pending_dca_order_id)
                logger.info("[CANCEL_DCA] %s pending orderId=%d", symbol, pos.pending_dca_order_id)
            except Exception:
                pass
            pos.pending_dca_order_id = 0

        # Cancel legacy DCA orders (grid-based approach)
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

        # Fallback: cancel ALL open orders on exchange for this symbol
        try:
            self._client.cancel_all_orders(symbol)
            logger.info("[CANCEL_ALL] %s — all exchange orders cancelled", symbol)
        except Exception:
            pass

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

            # DCA/TP fill detection REMOVED — handled by WS User Data Stream
            # (process_order_fill is the single source of truth for fills)

        return completed

    # ------------------------------------------------------------------
    # WS Fill Processing — Single Source of Truth
    # ------------------------------------------------------------------

    def process_order_fill(self, order_data: dict) -> list[LiveTrade]:
        """WS ORDER_TRADE_UPDATE handler. SINGLE ENTRY POINT for all fills.

        Idempotent: safe to call multiple times for the same orderId.
        Called from WS callback, immediate fill handling, and catch-up poll.

        order_data: {orderId, symbol, avgPrice, executedQty, ...}
        Returns: list of completed LiveTrade (for TP/SL exits)
        """
        order_id = order_data.get("orderId", 0)
        if order_id <= 0:
            return []

        # Idempotency guard
        if order_id in self._processed_order_ids:
            return []
        self._processed_order_ids.add(order_id)
        # Bound the set — remove IDs older than 24h by keeping only recent ones
        # Use simple size cap: if >5000, clear all but keep last 2000 by ID value
        # (higher order IDs are newer on Binance)
        if len(self._processed_order_ids) > 5000:
            keep = set(sorted(self._processed_order_ids)[-2000:])
            self._processed_order_ids = keep

        # WS health tracking
        self._ws_last_event_ts = time.time()

        # Lookup: which position's order is this?
        mapping = self._order_id_map.get(order_id)
        if not mapping:
            logger.debug("[FILL_SKIP] orderId=%d not in order_id_map (may be manual)", order_id)
            return []

        pos_key, order_type = mapping
        pos = self._positions.get(pos_key)
        if not pos or pos.condition == 0.0:
            logger.warning("[FILL_STALE] orderId=%d pos_key=%s — position closed/gone", order_id, pos_key)
            return []

        fill_price = float(order_data.get("avgPrice", 0) or 0)
        fill_qty = float(order_data.get("executedQty", 0) or 0)

        # Guard: invalid fill data
        if fill_price <= 0 or fill_qty <= 0:
            logger.error("[FILL_INVALID] orderId=%d price=%.4f qty=%.6f — skipping",
                         order_id, fill_price, fill_qty)
            return []

        trades: list[LiveTrade] = []
        if order_type == "DCA":
            self._process_dca_fill_live(pos_key, pos, fill_price, fill_qty, order_id)
        elif order_type == "TP":
            trades = self._process_tp_fill_live(pos_key, pos, fill_price, fill_qty, order_id)
        elif order_type == "SL":
            trades = self._process_sl_fill_live(pos_key, pos, fill_price, fill_qty, order_id)
        elif order_type == "ENTRY":
            pass  # Entry already handled by process_signal

        # Fill log (debug endpoint)
        self._fill_log.append({
            "time": time.strftime("%H:%M:%S"),
            "orderId": order_id,
            "symbol": order_data.get("symbol", pos.symbol),
            "type": order_type,
            "price": fill_price,
            "qty": fill_qty,
            "dca_count": pos.dca_fills_count,
            "bot_qty": self._position_qty.get(pos_key, 0),
        })
        if len(self._fill_log) > 50:
            self._fill_log = self._fill_log[-50:]

        # Invariant assertion
        self._assert_invariants(pos_key, pos)

        return trades

    def _process_dca_fill_live(self, key: str, pos: PositionState,
                                fill_price: float, fill_qty: float, order_id: int,
                                place_next_orders: bool = True) -> None:
        """Process confirmed DCA fill. Updates state + syncs qty.

        place_next_orders=False when called from immediate fill (prevents recursion).
        """
        tf_label = self._tf_labels.get(key, "")
        risk_mgr = self._get_risk_mgr(tf_label)

        # Dynamic comp margin
        if self._dyncomp_enabled and self._dyncomp_tiers:
            comp_pct = get_dynamic_comp_pct(self.balance, self._dyncomp_tiers)
            dca_margin = calc_step_margin(self.balance, comp_pct)
        else:
            dca_margin = pos.margin_per_step
        pos.margin_per_step = dca_margin

        risk_mgr.process_dca_fill(pos, fill_price)
        if pos.entry_atr > 0:
            risk_mgr.update_hard_stop(pos, pos.entry_atr)

        self.total_fees += fill_qty * fill_price * self._maker_fee
        pos.pending_dca_order_id = 0
        # Update qty: add fill_qty immediately, then try exchange sync
        self._position_qty[key] = self._position_qty.get(key, 0) + fill_qty
        self._sync_position_qty(key, pos)

        logger.info("[DCA_FILL] %s #%d @ %.4f qty=%.6f | dca=%d/%d | bot_qty=%.6f",
                    pos.symbol, order_id, fill_price, fill_qty,
                    pos.dca_fills_count, risk_mgr._max_dca_steps,
                    self._position_qty.get(key, 0))

        if place_next_orders:
            # Place TP for new position size (order must be on exchange)
            self._place_tp_order(key, pos)
            # Do NOT place next DCA here — wait for next candle KC update
            # Backtest parity: each candle max 1 DCA (if block, not loop)

    def _process_tp_fill_live(self, key: str, pos: PositionState,
                               fill_price: float, fill_qty: float,
                               order_id: int,
                               place_next_orders: bool = True) -> list[LiveTrade]:
        """Process confirmed TP fill. Updates state + records trade.

        place_next_orders=False when called from immediate fill (prevents recursion).
        """
        tf_label = self._tf_labels.get(key, "")
        risk_mgr = self._get_risk_mgr(tf_label)
        trades: list[LiveTrade] = []

        avg_before = pos.average_entry_price if pos.average_entry_price > 0 else fill_price
        closed_notional = risk_mgr.process_tp_fill(pos, fill_price)
        pnl_usdt = 0.0
        pnl_pct = 0.0

        if closed_notional > 0:
            self._trade_counter += 1
            if pos.side == "LONG":
                pnl_pct = (fill_price - avg_before) / avg_before * 100
            else:
                pnl_pct = (avg_before - fill_price) / avg_before * 100
            pnl_usdt = closed_notional * pnl_pct / 100
            tp_fee = closed_notional * self._maker_fee
            self._update_stats(pnl_usdt, tp_fee)

            trade = LiveTrade(
                id=self._trade_counter, symbol=pos.symbol, side=pos.side,
                entry_price=avg_before, entry_time=pos.entry_time,
                exit_price=fill_price, exit_time=int(time.time() * 1000),
                exit_reason="TP", qty=fill_qty, qty_usdt=round(closed_notional, 2),
                leverage=pos.leverage, pnl_usdt=round(pnl_usdt, 4),
                pnl_percent=round(pnl_pct, 4), fee_usdt=round(tp_fee, 4),
                order_id=order_id,
            )
            self.trades.append(trade)
            trades.append(trade)

        pos.tp_order_id = 0
        self._sync_position_qty(key, pos)

        # Check if remaining position is dust (< $10 notional) → close entirely
        remaining_qty = self._position_qty.get(key, 0)
        if pos.condition != 0.0 and remaining_qty > 0:
            dust_notional = remaining_qty * fill_price
            if dust_notional < 10.0:
                logger.info("[DUST_CLOSE] %s remaining qty=%.6f notional=$%.2f — marking closed",
                            pos.symbol, remaining_qty, dust_notional)
                try:
                    close_side = "SELL" if pos.side == "LONG" else "BUY"
                    self._client.market_order(pos.symbol, close_side, remaining_qty, reduce_only=True)
                except Exception:
                    pass  # Dust too small to close on exchange — that's OK
                pos.condition = 0.0
                pos.remaining_qty = 0.0
                self._position_qty[key] = 0
                self._cancel_all_grid_orders(key, pos)
                logger.info("[POS_CLOSED] %s dust closed", pos.symbol)
                return trades

        logger.info("[TP_FILL] %s #%d @ %.4f qty=%.6f pnl=$%.2f | dca=%d | bot_qty=%.6f",
                    pos.symbol, order_id, fill_price, fill_qty,
                    pnl_usdt if closed_notional > 0 else 0,
                    pos.dca_fills_count, remaining_qty)

        if pos.condition == 0.0:
            self._cancel_all_grid_orders(key, pos)
            logger.info("[POS_CLOSED] %s fully closed via TP", pos.symbol)
        elif place_next_orders:
            # Before placing next orders, re-check dust (sync may have updated qty)
            final_qty = self._position_qty.get(key, 0)
            if final_qty > 0 and final_qty * fill_price < 10.0:
                logger.info("[DUST_CLOSE] %s post-sync dust qty=%.6f — marking closed", pos.symbol, final_qty)
                pos.condition = 0.0
                pos.remaining_qty = 0.0
                self._position_qty[key] = 0
                self._cancel_all_grid_orders(key, pos)
            # else: Do NOT place new DCA/TP here — wait for next candle KC update
            # Backtest parity: elif means TP fill candle does not place new DCA/TP
            # Next scanner cycle will compute fresh KC bands and place orders

        return trades

    def _process_sl_fill_live(self, key: str, pos: PositionState,
                               fill_price: float, fill_qty: float,
                               order_id: int) -> list[LiveTrade]:
        """Process SL stop-market fill detected via WS.

        Position is already closed on exchange by the SL order.
        Do NOT send another market close — just update internal state and record trade.
        """
        trades: list[LiveTrade] = []

        # Cancel remaining DCA/TP orders (position is closed, orders are orphaned)
        self._cancel_all_grid_orders(key, pos)

        # Record trade without sending another market order
        self._trade_counter += 1
        avg_e = pos.average_entry_price if pos.average_entry_price > 0 else fill_price
        if pos.side == "LONG":
            pnl_pct = (fill_price - avg_e) / avg_e * 100 if avg_e > 0 else 0
        else:
            pnl_pct = (avg_e - fill_price) / avg_e * 100 if avg_e > 0 else 0
        notional = fill_qty * avg_e
        pnl_usdt = notional * pnl_pct / 100
        exit_fee = fill_qty * fill_price * self._taker_fee
        self._update_stats(pnl_usdt, exit_fee)

        trade = LiveTrade(
            id=self._trade_counter, symbol=pos.symbol, side=pos.side,
            entry_price=pos.average_entry_price, entry_time=pos.entry_time,
            exit_price=fill_price, exit_time=int(time.time() * 1000),
            exit_reason="SL_WS", qty=fill_qty,
            qty_usdt=round(notional, 2), leverage=pos.leverage,
            pnl_usdt=round(pnl_usdt, 4), pnl_percent=round(pnl_pct, 4),
            fee_usdt=round(exit_fee, 4), order_id=order_id,
        )
        self.trades.append(trade)
        trades.append(trade)

        # Mark position closed internally
        pos.condition = 0.0
        pos.remaining_qty = 0.0
        self._position_qty[key] = 0
        self._sl_order_ids.pop(key, None)

        logger.warning("[SL_FILL_WS] %s #%d @ %.4f pnl=$%.2f — position closed (no duplicate close)",
                       pos.symbol, order_id, fill_price, pnl_usdt)
        return trades

    def _sync_position_qty(self, key: str, pos: PositionState) -> None:
        """Sync _position_qty[key] with actual Binance exchange position.
        Exchange is the single source of truth.
        Rate-limited: max 1 call per 2 seconds to avoid API exhaustion.
        """
        now = time.time()
        if now - getattr(self, '_last_qty_sync_ts', 0) < 2.0:
            return  # Skip — too frequent, rely on fill qty from WS
        self._last_qty_sync_ts = now

        symbol = pos.symbol
        try:
            positions = self._client.get_positions()
            for ep in positions:
                if ep["symbol"] == symbol:
                    exchange_qty = ep["amount"]
                    bot_qty = self._position_qty.get(key, 0)
                    if abs(exchange_qty - bot_qty) > 0.0001:
                        logger.info("[QTY_SYNC] %s: bot=%.6f → exchange=%.6f",
                                    symbol, bot_qty, exchange_qty)
                    self._position_qty[key] = exchange_qty
                    return
            if pos.condition != 0.0:
                logger.warning("[QTY_SYNC] %s not found on exchange but bot has position", symbol)
                self._position_qty[key] = 0
        except Exception as e:
            logger.error("[QTY_SYNC] Failed for %s: %s", symbol, e)

    def _assert_invariants(self, key: str, pos: PositionState) -> None:
        """Post-fill invariant check. Triggers circuit breaker on violation."""
        tf_label = self._tf_labels.get(key, "")
        risk_mgr = self._get_risk_mgr(tf_label)
        violations = []

        if pos.dca_fills_count > risk_mgr._max_dca_steps:
            violations.append(f"dca_count={pos.dca_fills_count} > max={risk_mgr._max_dca_steps}")
        if pos.dca_fills_count < 0:
            violations.append(f"dca_count={pos.dca_fills_count} < 0")
        if pos.condition != 0.0 and self._position_qty.get(key, 0) <= 0:
            violations.append(f"open position but qty={self._position_qty.get(key, 0)}")
        if pos.condition != 0.0 and pos.total_position_notional <= 0:
            violations.append(f"open position but notional={pos.total_position_notional}")

        if violations:
            for v in violations:
                logger.critical("[INVARIANT] %s %s: %s", pos.symbol, key, v)
            self.circuit_breaker_triggered = True
            self.circuit_breaker_reason = f"Invariant violation: {violations[0]}"

    def _catchup_poll_all_orders(self) -> None:
        """WS disconnect recovery — poll all pending orders for missed fills."""
        logger.info("[CATCHUP] Polling all pending orders for missed fills...")
        for key, pos in list(self._positions.items()):
            if pos.condition == 0.0:
                continue
            # Check DCA order
            if pos.pending_dca_order_id > 0:
                try:
                    order = self._client.get_order(pos.symbol, pos.pending_dca_order_id)
                    if order.get("status") == "FILLED":
                        self.process_order_fill({
                            "orderId": pos.pending_dca_order_id,
                            "symbol": pos.symbol,
                            "avgPrice": float(order.get("avgPrice", 0)),
                            "executedQty": float(order.get("executedQty", 0)),
                        })
                except Exception as e:
                    logger.error("[CATCHUP] DCA check failed %s: %s", pos.symbol, e)
            # Check TP order
            if getattr(pos, 'tp_order_id', 0) > 0:
                try:
                    order = self._client.get_order(pos.symbol, pos.tp_order_id)
                    if order.get("status") == "FILLED":
                        self.process_order_fill({
                            "orderId": pos.tp_order_id,
                            "symbol": pos.symbol,
                            "avgPrice": float(order.get("avgPrice", 0)),
                            "executedQty": float(order.get("executedQty", 0)),
                        })
                except Exception as e:
                    logger.error("[CATCHUP] TP check failed %s: %s", pos.symbol, e)
            # Check SL order
            sl_id = self._sl_order_ids.get(key, 0)
            if sl_id > 0:
                try:
                    order = self._client.get_order(pos.symbol, sl_id)
                    if order.get("status") == "FILLED":
                        self.process_order_fill({
                            "orderId": sl_id,
                            "symbol": pos.symbol,
                            "avgPrice": float(order.get("avgPrice", 0)),
                            "executedQty": float(order.get("executedQty", 0)),
                        })
                except Exception as e:
                    logger.error("[CATCHUP] SL check failed %s: %s", pos.symbol, e)

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
        """Emergency: close all positions and cancel ALL orders immediately."""
        all_trades: list[LiveTrade] = []

        # Step 1: Cancel ALL open orders on exchange for every symbol
        all_symbols = set()
        for key, pos in list(self._positions.items()):
            if pos.condition != 0.0:
                all_symbols.add(pos.symbol)
        for sym in self._pair_configs:
            all_symbols.add(sym)

        for sym in all_symbols:
            try:
                self._client.cancel_all_orders(sym)
                logger.info("[EMERGENCY] Cancelled all orders for %s", sym)
            except Exception as e:
                logger.error("[EMERGENCY] Cancel orders failed for %s: %s", sym, e)

        # Step 2: Get current prices
        current_prices: dict[str, float] = {}
        for sym in all_symbols:
            try:
                ticker = self._client._request(
                    "GET", "/fapi/v1/ticker/price",
                    {"symbol": sym}, signed=False,
                )
                current_prices[sym] = float(ticker.get("price", 0))
            except Exception:
                pass

        # Step 3: Close all positions
        for key in list(self._positions.keys()):
            pos = self._positions[key]
            if pos.condition != 0.0:
                price = current_prices.get(pos.symbol) or pos.entry_price
                trades = self._close_position(key, price)
                all_trades.extend(trades)
        return all_trades
