"""Volume-Maximizing Market Making Strategy for Loaf Markets.

This strategy:
1. Targets 70% portfolio capital utilization (keeping a 30% risk reserve).
2. Maximizes trading volume through tight two-sided limit quoting (spread capture
   and price improvement).
3. Automatically balances inventory (USDL <-> Tokens) to sustain continuous
   high-frequency volume generation.
4. Enforces strict exchange constraints (min 10 USDL notional, 1 decimal quantity,
   2 decimals price, fresh nonces, wash trade prevention).
5. Robust fill tracking via both private WebSocket and periodic REST order book synchronization.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import loaf
from loaf import LoafClient
from loaf.enums import OrderSide, OrderType, TimeInForce
from loaf.exceptions import (
    CompetitionEligibilityError,
    KycRequiredError,
    LoafAPIError,
    LoafConflictError,
    LoafRateLimitError,
    LoafValidationError,
    TradingHaltedError,
)
from loaf.money import validate_price, validate_quantity

logger = logging.getLogger("loaf.bot.strategy")


@dataclass
class StrategyConfig:
    """Configuration parameters for the Volume Maximizer Strategy."""

    target_token: str
    portfolio_pct: float = 0.70  # 70% portfolio utilization target
    fallback_price: float | None = None  # Reference price from metadata to prevent startup mispricing
    min_order_notional: float = 10.0  # USDL minimum per order
    spread_bps: float = 20.0  # 0.20% default spread offset when book is empty/one-sided
    min_tick: float = 0.01  # Minimum price tick ($0.01)
    min_qty_step: float = 0.1  # Minimum quantity step (0.1 token)
    order_tranches: int = 1  # Number of order tranches per side (1 for fast top-of-book)
    tick_interval: float = 2.0  # Main decision loop interval in seconds
    stale_order_timeout: float = 12.0  # Refresh resting orders if older than X seconds
    price_deviation_threshold: float = 0.002  # 0.2% price move triggers quote replacement
    inventory_skew_threshold: float = 0.15  # 15% imbalance triggers aggressive rebalancing
    rest_sync_interval: float = 4.0  # Seconds between REST state polling for fills
    dry_run: bool = False  # Simulate orders without hitting exchange


@dataclass
class OrderTracker:
    """Tracks a resting order placed by this bot."""

    order_id: int | str
    side: str
    token_name: str
    price: float
    quantity: float
    created_at: float = field(default_factory=time.time)
    quantity_left: float | None = None
    status: str = "OPEN"


@dataclass
class StrategyMetrics:
    """Telemetry and volume statistics for the current trading session."""

    session_start_time: float = field(default_factory=time.time)
    session_buy_volume: float = 0.0  # USDL bought
    session_sell_volume: float = 0.0  # USDL sold
    total_trades_count: int = 0
    orders_placed_count: int = 0
    orders_cancelled_count: int = 0
    orders_rejected_count: int = 0
    lifetime_volume: float = 0.0
    initial_equity: float = 0.0
    current_equity: float = 0.0
    current_cash: float = 0.0
    current_frozen: float = 0.0
    current_token_qty: float = 0.0
    current_token_val: float = 0.0
    active_utilization_pct: float = 0.0

    @property
    def total_session_volume(self) -> float:
        """Total volume generated in USDL during this session."""
        return self.session_buy_volume + self.session_sell_volume

    @property
    def session_pnl(self) -> float:
        """PnL in USDL since bot started."""
        if self.initial_equity <= 0:
            return 0.0
        return self.current_equity - self.initial_equity

    @property
    def session_pnl_pct(self) -> float:
        """PnL % since bot started."""
        if self.initial_equity <= 0:
            return 0.0
        return (self.session_pnl / self.initial_equity) * 100.0


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Extracts a property or dict key seamlessly from LoafObject or dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, key):
        val = getattr(obj, key)
        return val if val is not None else default
    if hasattr(obj, "get"):
        return obj.get(key, default)
    return default


class VolumeMaximizerStrategy:
    """High-Turnover Volume Maximizing Strategy targeting 70% Portfolio Utilization."""

    def __init__(self, client: LoafClient, config: StrategyConfig) -> None:
        self.client = client
        self.config = config
        self.token_name = config.target_token
        self.fallback_price = config.fallback_price

        self._lock = threading.Lock()

        # Market data feeds
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.mark_price: float | None = None
        self.last_trade_price: float | None = None

        # Portfolio state
        self.cash: float = 0.0
        self.frozen_cash: float = 0.0
        self.token_quantity: float = 0.0
        self.total_token_quantity: float = 0.0
        self.portfolio_value: float = 0.0
        self.portfolio_pnl: float = 0.0

        # Active order tracking
        self.active_orders: dict[int | str, OrderTracker] = {}
        self.active_buy_orders: dict[int | str, OrderTracker] = {}
        self.active_sell_orders: dict[int | str, OrderTracker] = {}

        # Strategy telemetry
        self.metrics = StrategyMetrics()
        self._last_tick_time: float = 0.0
        self._last_status_print: float = 0.0
        self._last_rest_sync: float = 0.0
        self._halted: bool = False
        self._ineligible: bool = False

    # ----------------------------------------------------------------------- #
    # Portfolio & Sizing Math (70% Target Allocation)
    # ----------------------------------------------------------------------- #

    def calculate_portfolio_state(self) -> dict[str, float]:
        """Calculates current equity, 70% capital budget, and active utilization."""
        with self._lock:
            free_cash = self.cash
            frozen_cash = self.frozen_cash
            free_qty = self.token_quantity
            ref_price = self.get_reference_price()

            # Track locally committed capital in active resting orders
            committed_buy_usd = sum(
                round(o.price * o.quantity, 2) for o in self.active_buy_orders.values()
            )
            committed_sell_tokens = sum(
                round(o.quantity, 1) for o in self.active_sell_orders.values()
            )

            # Total tokens: unfrozen tokens + tokens locked in active sell orders
            total_tokens = max(self.total_token_quantity, free_qty + committed_sell_tokens)

        token_value = round(total_tokens * ref_price, 2)
        total_equity = round(free_cash + frozen_cash + token_value, 2)

        if total_equity <= 0:
            total_equity = round(max(self.portfolio_value, 0.0), 2)

        # 70% active capital target budget & 30% reserve
        target_budget = round(total_equity * self.config.portfolio_pct, 2)
        reserve_budget = round(total_equity * (1.0 - self.config.portfolio_pct), 2)

        # Capital currently committed: active buy orders / frozen cash + token inventory value
        currently_committed = round(max(frozen_cash, committed_buy_usd) + token_value, 2)
        utilization_pct = (currently_committed / total_equity) if total_equity > 0 else 0.0

        # Total deployable capital across resting and new BUY orders (capped at 70% target budget)
        total_cash_pool = free_cash + committed_buy_usd
        max_buy_budget = max(0.0, round(target_budget - token_value, 2))
        deployable_buy_usd = min(total_cash_pool, max_buy_budget)

        # Total deployable tokens across resting and new SELL orders
        deployable_sell_tokens = total_tokens

        with self._lock:
            self.metrics.current_equity = total_equity
            self.metrics.current_cash = free_cash
            self.metrics.current_frozen = frozen_cash
            self.metrics.current_token_qty = total_tokens
            self.metrics.current_token_val = token_value
            self.metrics.active_utilization_pct = utilization_pct * 100.0
            if self.metrics.initial_equity <= 0 and total_equity > 0:
                self.metrics.initial_equity = total_equity

        return {
            "total_equity": total_equity,
            "target_budget": target_budget,
            "reserve_budget": reserve_budget,
            "currently_committed": currently_committed,
            "utilization_pct": utilization_pct,
            "free_cash": free_cash,
            "frozen_cash": frozen_cash,
            "free_tokens": free_qty,
            "total_tokens": total_tokens,
            "available_cash": deployable_buy_usd,
            "available_tokens": deployable_sell_tokens,
            "token_value": token_value,
            "ref_price": ref_price,
        }

    def get_reference_price(self) -> float:
        """Returns the best available reference price for the token."""
        if self.best_bid is not None and self.best_ask is not None and self.best_bid > 0 and self.best_ask > 0:
            return round((self.best_bid + self.best_ask) / 2.0, 2)
        if self.mark_price is not None and self.mark_price > 0:
            return round(self.mark_price, 2)
        if self.last_trade_price is not None and self.last_trade_price > 0:
            return round(self.last_trade_price, 2)
        if self.best_bid is not None and self.best_bid > 0:
            return round(self.best_bid, 2)
        if self.best_ask is not None and self.best_ask > 0:
            return round(self.best_ask, 2)
        if self.fallback_price is not None and self.fallback_price > 0:
            return round(self.fallback_price, 2)
        return 1.0

    # ----------------------------------------------------------------------- #
    # Quote Calculation & Price Improvement Logic
    # ----------------------------------------------------------------------- #

    def compute_quotes(
        self,
        best_bid: float | None,
        best_ask: float | None,
        ref_price: float,
        available_cash: float,
        available_tokens: float,
        target_budget: float,
        current_token_val: float,
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Calculates optimal limit buy and sell quote price & quantity pairs.

        Returns:
            (buy_quotes, sell_quotes) where each quote is (price, quantity).
        """
        buy_quotes: list[tuple[float, float]] = []
        sell_quotes: list[tuple[float, float]] = []

        if ref_price is None or ref_price <= 0:
            return buy_quotes, sell_quotes

        tick = self.config.min_tick

        # Determine target buy and sell prices based on order book spread
        if best_bid is not None and best_ask is not None and best_ask > best_bid:
            spread = round(best_ask - best_bid, 2)
            if spread > 2 * tick:
                # Wide spread: price improve by 1 tick on both sides to be #1 in queue
                buy_price = round(best_bid + tick, 2)
                sell_price = round(best_ask - tick, 2)
            elif spread == 2 * tick:
                # Spread is $0.02: join top of book
                buy_price = round(best_bid, 2)
                sell_price = round(best_ask, 2)
            else:
                # Spread is tight ($0.01): join top of book
                buy_price = round(best_bid, 2)
                sell_price = round(best_ask, 2)
        elif best_bid is not None and best_bid > 0:
            # One-sided (bids only)
            buy_price = round(best_bid, 2)
            offset = max(tick, round(buy_price * (self.config.spread_bps / 10000.0), 2))
            sell_price = round(buy_price + offset, 2)
        elif best_ask is not None and best_ask > 0:
            # One-sided (asks only)
            sell_price = round(best_ask, 2)
            offset = max(tick, round(sell_price * (self.config.spread_bps / 10000.0), 2))
            buy_price = max(tick, round(sell_price - offset, 2))
        else:
            # Empty book: quote around reference price
            offset = max(tick, round(ref_price * (self.config.spread_bps / 10000.0), 2))
            buy_price = max(tick, round(ref_price - offset, 2))
            sell_price = round(ref_price + offset, 2)

        # STRICT INVARIANT: Never cross own quotes (buy_price < sell_price)
        if buy_price >= sell_price:
            sell_price = round(buy_price + tick, 2)

        # Inventory Balancing Logic:
        # Target inventory is 50% of the 70% active capital budget in tokens and 50% in USDL.
        half_budget = target_budget / 2.0
        token_imbalance = current_token_val - half_budget
        imbalance_pct = (token_imbalance / target_budget) if target_budget > 0 else 0.0

        # Calculate budget allocation for BUY and SELL sides
        if imbalance_pct > self.config.inventory_skew_threshold:
            # Overweight tokens: heavy selling, light buying
            buy_alloc = min(available_cash, half_budget * 0.5)
            sell_token_alloc = available_tokens
        elif imbalance_pct < -self.config.inventory_skew_threshold:
            # Underweight tokens: heavy buying, light selling
            buy_alloc = available_cash
            sell_token_alloc = available_tokens
        else:
            # Balanced: quote both sides evenly up to available resources
            buy_alloc = available_cash
            sell_token_alloc = available_tokens

        # Sizing for BUY quotes
        if buy_alloc >= self.config.min_order_notional and buy_price > 0:
            tranches = max(1, self.config.order_tranches)
            per_tranche_usd = buy_alloc / tranches

            for i in range(tranches):
                p = round(buy_price - (i * tick), 2)
                if p <= 0:
                    continue
                raw_qty = per_tranche_usd / p
                # Floor to 1 decimal place (0.1 token)
                qty = math.floor(raw_qty * 10) / 10.0
                qty = round(qty, 1)

                if qty >= self.config.min_qty_step and (qty * p) >= self.config.min_order_notional:
                    buy_quotes.append((p, qty))

        # Sizing for SELL quotes
        if sell_token_alloc > 0 and sell_price > 0:
            tranches = max(1, self.config.order_tranches)
            per_tranche_qty = sell_token_alloc / tranches

            for i in range(tranches):
                p = round(sell_price + (i * tick), 2)
                raw_qty = per_tranche_qty
                # Floor to 1 decimal place
                qty = math.floor(raw_qty * 10) / 10.0
                qty = round(qty, 1)

                if qty >= self.config.min_qty_step and (qty * p) >= self.config.min_order_notional:
                    sell_quotes.append((p, qty))
                elif qty > 0 and qty == available_tokens:
                    # Closing entire position waives 10 USDL minimum
                    sell_quotes.append((p, qty))

        return buy_quotes, sell_quotes

    # ----------------------------------------------------------------------- #
    # Order Reconciliation & Execution
    # ----------------------------------------------------------------------- #

    def reconcile_orders(
        self,
        desired_buys: list[tuple[float, float]],
        desired_sells: list[tuple[float, float]],
    ) -> None:
        """Compares desired quotes with resting orders, cancelling stale ones and placing new ones."""
        now = time.time()

        # 1. Inspect existing active buy orders
        active_buys = list(self.active_buy_orders.values())
        for tracker in active_buys:
            age = now - tracker.created_at
            price_drift = False
            if desired_buys:
                best_desired_price = desired_buys[0][0]
                drift = abs(tracker.price - best_desired_price) / best_desired_price
                if drift >= self.config.price_deviation_threshold:
                    price_drift = True

            if age >= self.config.stale_order_timeout or price_drift or not desired_buys:
                self._cancel_order(tracker)

        # 2. Inspect existing active sell orders
        active_sells = list(self.active_sell_orders.values())
        for tracker in active_sells:
            age = now - tracker.created_at
            price_drift = False
            if desired_sells:
                best_desired_price = desired_sells[0][0]
                drift = abs(tracker.price - best_desired_price) / best_desired_price
                if drift >= self.config.price_deviation_threshold:
                    price_drift = True

            if age >= self.config.stale_order_timeout or price_drift or not desired_sells:
                self._cancel_order(tracker)

        # 3. Place new BUY orders if we don't already have matching active orders
        if desired_buys and not self.active_buy_orders:
            for price, desired_qty in desired_buys:
                free_cash = self.cash
                if free_cash < self.config.min_order_notional:
                    logger.debug("Free cash ($%.2f) below minimum notional ($%.2f). Waiting for funds/cancels.", free_cash, self.config.min_order_notional)
                    continue

                notional = price * desired_qty
                if free_cash >= notional:
                    qty = desired_qty
                else:
                    qty = math.floor((free_cash / price) * 10) / 10.0

                if qty >= self.config.min_qty_step and (qty * price) >= self.config.min_order_notional:
                    self._place_order(OrderSide.BUY, price, qty)

        # 4. Place new SELL orders if we don't already have matching active orders
        if desired_sells and not self.active_sell_orders:
            for price, desired_qty in desired_sells:
                free_tokens = self.token_quantity
                if free_tokens < self.config.min_qty_step:
                    logger.debug("Free tokens (%.1f) below minimum step (%.1f). Waiting for fills.", free_tokens, self.config.min_qty_step)
                    continue

                if free_tokens >= desired_qty:
                    qty = desired_qty
                else:
                    qty = math.floor(free_tokens * 10) / 10.0

                if qty >= self.config.min_qty_step and ((qty * price) >= self.config.min_order_notional or qty == free_tokens):
                    self._place_order(OrderSide.SELL, price, qty)

    def _place_order(self, side: str, price: float, quantity: float) -> Any:
        """Places a single limit order using the nonce dance with full error handling."""
        price = round(price, 2)
        quantity = round(quantity, 1)

        try:
            validate_price(price)
            validate_quantity(quantity)
        except LoafValidationError as e:
            logger.warning("Local order validation failed: %s (price=%s, qty=%s)", e, price, quantity)
            return None

        notional = round(price * quantity, 2)
        logger.info(
            "[%s] Submitting LIMIT %s: %s tokens @ $%s (notional: $%s)",
            self.token_name,
            side,
            quantity,
            price,
            notional,
        )

        if self.config.dry_run:
            sim_id = f"sim_{int(time.time() * 1000)}"
            tracker = OrderTracker(
                order_id=sim_id,
                side=side,
                token_name=self.token_name,
                price=price,
                quantity=quantity,
            )
            self.active_orders[sim_id] = tracker
            if side == OrderSide.BUY:
                self.active_buy_orders[sim_id] = tracker
                with self._lock:
                    self.cash = max(0.0, round(self.cash - notional, 2))
                    self.frozen_cash = round(self.frozen_cash + notional, 2)
            else:
                self.active_sell_orders[sim_id] = tracker
                with self._lock:
                    self.token_quantity = max(0.0, round(self.token_quantity - quantity, 1))
            self.metrics.orders_placed_count += 1
            logger.info("[DRY RUN] Simulated order %s accepted.", sim_id)
            return sim_id

        try:
            placed = self.client.orders.create(
                token_name=self.token_name,
                side=side,
                quantity=quantity,
                type=OrderType.LIMIT,
                price=price,
                time_in_force=TimeInForce.GTC,
            )
            order_id = placed.orderId
            tracker = OrderTracker(
                order_id=order_id,
                side=side,
                token_name=self.token_name,
                price=price,
                quantity=quantity,
            )
            self.active_orders[order_id] = tracker
            if side == OrderSide.BUY:
                self.active_buy_orders[order_id] = tracker
                with self._lock:
                    self.cash = max(0.0, round(self.cash - notional, 2))
                    self.frozen_cash = round(self.frozen_cash + notional, 2)
            else:
                self.active_sell_orders[order_id] = tracker
                with self._lock:
                    self.token_quantity = max(0.0, round(self.token_quantity - quantity, 1))

            self.metrics.orders_placed_count += 1
            logger.info("Order placed successfully. orderId=%s", order_id)
            return placed
        except CompetitionEligibilityError:
            self._ineligible = True
            logger.warning(
                "Cannot place order: account not admitted to the active competition round."
            )
        except TradingHaltedError:
            self._halted = True
            logger.warning("Cannot place order: trading is currently halted platform-wide.")
        except KycRequiredError:
            logger.error("Cannot place order: KYC verification is required.")
        except LoafRateLimitError as exc:
            logger.warning("Rate limit hit during order placement: retry after %ss", exc.retry_after)
            time.sleep(exc.retry_after or 1.0)
        except LoafConflictError as exc:
            logger.warning("Order conflict: %s", exc.message)
        except LoafAPIError as exc:
            self.metrics.orders_rejected_count += 1
            logger.error("Order rejected by exchange: %s (status %s)", exc.message, exc.status_code)
        except Exception as exc:
            self.metrics.orders_rejected_count += 1
            logger.error("Unexpected error placing order: %s", exc)
        return None

    def _cancel_order(self, tracker: OrderTracker) -> bool:
        """Cancels a resting order and removes it from active tracking."""
        order_id = tracker.order_id
        logger.info("[%s] Cancelling %s order #%s (@ $%s)", self.token_name, tracker.side, order_id, tracker.price)

        if self.config.dry_run or str(order_id).startswith("sim_"):
            if tracker.side == OrderSide.BUY:
                notional = round(tracker.price * tracker.quantity, 2)
                with self._lock:
                    self.cash += notional
                    self.frozen_cash = max(0.0, round(self.frozen_cash - notional, 2))
            else:
                with self._lock:
                    self.token_quantity += tracker.quantity
            self._remove_tracked_order(order_id)
            self.metrics.orders_cancelled_count += 1
            return True

        try:
            self.client.orders.cancel(int(order_id))
            self._remove_tracked_order(order_id)
            self.metrics.orders_cancelled_count += 1
            return True
        except LoafConflictError:
            self._remove_tracked_order(order_id)
            return True
        except LoafAPIError as exc:
            msg_str = str(exc.message).lower()
            # If server indicates the order was already filled or closed, handle cleanly
            if "status: filled" in msg_str or "status: cancelled" in msg_str or "not found" in msg_str or exc.status_code in (400, 404, 409):
                self._remove_tracked_order(order_id)
                if "filled" in msg_str:
                    fill_notional = round(tracker.price * tracker.quantity, 2)
                    self.metrics.total_trades_count += 1
                    if tracker.side == OrderSide.BUY:
                        self.metrics.session_buy_volume += fill_notional
                    else:
                        self.metrics.session_sell_volume += fill_notional
                    logger.info("[%s] Order #%s was FILLED on exchange (+$%s volume). Tracking updated.", self.token_name, order_id, fill_notional)
                else:
                    logger.info("[%s] Order #%s is no longer open on exchange. Tracking updated.", self.token_name, order_id)
                return True
            logger.warning("Error cancelling order #%s: %s", order_id, exc)
            return False
        except TradingHaltedError:
            logger.warning("Cannot cancel order: trading is halted.")
            return False
        except Exception as exc:
            msg_str = str(exc).lower()
            if "filled" in msg_str or "cancelled" in msg_str or "400" in msg_str or "404" in msg_str or "409" in msg_str:
                self._remove_tracked_order(order_id)
                return True
            logger.warning("Error cancelling order #%s: %s", order_id, exc)
            return False

    def _remove_tracked_order(self, order_id: int | str) -> None:
        """Safely removes an order from all active trackers."""
        self.active_orders.pop(order_id, None)
        self.active_buy_orders.pop(order_id, None)
        self.active_sell_orders.pop(order_id, None)

    def cancel_all_orders(self) -> None:
        """Emergency cleanup: cancels all resting orders."""
        logger.info("Cancelling all active orders...")
        if self.config.dry_run:
            self.active_orders.clear()
            self.active_buy_orders.clear()
            self.active_sell_orders.clear()
            return

        try:
            self.client.orders.cancel_all()
        except Exception as e:
            logger.warning("cancel_all API call returned: %s", e)

        self.active_orders.clear()
        self.active_buy_orders.clear()
        self.active_sell_orders.clear()

    # ----------------------------------------------------------------------- #
    # Sync / Seed State from REST
    # ----------------------------------------------------------------------- #

    def sync_portfolio_from_rest(self) -> None:
        """Seeds balances and positions from REST portfolio endpoint."""
        try:
            comp = self.client.portfolio.component()
            with self._lock:
                self.cash = float(comp.cash)
                self.frozen_cash = float(comp.frozen)
                self.portfolio_value = float(comp.portfolioValue)
                self.portfolio_pnl = float(comp.portfolioPnl)
                if hasattr(comp, "lifetimeVolume") and comp.lifetimeVolume is not None:
                    self.metrics.lifetime_volume = float(comp.lifetimeVolume)

                positions = comp.get("positions") or []
                self.token_quantity = 0.0
                self.total_token_quantity = 0.0
                for pos in positions:
                    if pos.tokenName == self.token_name:
                        self.token_quantity = float(pos.quantity)
                        self.total_token_quantity = float(getattr(pos, "totalQuantity", pos.quantity))
                        break

            logger.debug(
                "Synced portfolio: Cash=$%.2f, Frozen=$%.2f, %s Qty=%.1f (Total=%.1f), TotalVal=$%.2f",
                self.cash,
                self.frozen_cash,
                self.token_name,
                self.token_quantity,
                self.total_token_quantity,
                self.portfolio_value,
            )
        except Exception as e:
            logger.debug("Could not sync portfolio from REST: %s", e)

    def sync_active_orders_from_rest(self) -> None:
        """Seeds open orders from REST active_orders endpoint and detects completed fills."""
        if self.config.dry_run:
            return
        try:
            res = self.client.history.active_orders()
            orders = res.get("activeOrders") or []
            active_ids = {o.orderId for o in orders}

            # Find tracked orders that are no longer active on the exchange
            stale_ids = [oid for oid in self.active_orders if isinstance(oid, int) and oid not in active_ids]
            for oid in stale_ids:
                tracker = self.active_orders.get(oid)
                if tracker:
                    fill_notional = round(tracker.price * tracker.quantity, 2)
                    self.metrics.total_trades_count += 1
                    if tracker.side == OrderSide.BUY:
                        self.metrics.session_buy_volume += fill_notional
                        logger.info("⚡ [REST FILL DETECTED] BUY %s %s @ $%s (+$%s volume)", tracker.quantity, tracker.token_name, tracker.price, fill_notional)
                    else:
                        self.metrics.session_sell_volume += fill_notional
                        logger.info("⚡ [REST FILL DETECTED] SELL %s %s @ $%s (+$%s volume)", tracker.quantity, tracker.token_name, tracker.price, fill_notional)
                self._remove_tracked_order(oid)
        except Exception as e:
            logger.debug("Could not sync active orders from REST: %s", e)

    # ----------------------------------------------------------------------- #
    # WebSocket Event Handlers
    # ----------------------------------------------------------------------- #

    def on_orderbook(self, msg: Any) -> None:
        """Updates best bid and ask from live orderbook WebSocket stream."""
        bids = _get_field(msg, "bids") or []
        asks = _get_field(msg, "asks") or []
        with self._lock:
            self.best_bid = float(_get_field(bids[0], "price")) if bids else None
            self.best_ask = float(_get_field(asks[0], "price")) if asks else None

    def on_mark_price(self, msg: Any) -> None:
        """Updates mark price from live WebSocket feed."""
        price = _get_field(msg, "price")
        if price is not None:
            with self._lock:
                self.mark_price = float(price)

    def on_trade_tick(self, msg: Any) -> None:
        """Updates last trade price from public trades stream."""
        trades = _get_field(msg, "trades") or []
        if trades:
            price = _get_field(trades[0], "price")
            if price is not None:
                with self._lock:
                    self.last_trade_price = float(price)

    def on_my_fill(self, msg: Any) -> None:
        """Handles private fill events on our own orders."""
        trade = _get_field(msg, "trade")
        if not trade:
            return

        side = str(_get_field(trade, "side", "")).upper()
        price = float(_get_field(trade, "price", 0.0))
        qty = float(_get_field(trade, "quantity", 0.0))
        token = _get_field(trade, "tokenName", self.token_name)
        notional = round(price * qty, 2)

        logger.info(
            "*** [WS FILL] %s %s tokens of %s @ $%s (Turnover: +$%s) ***",
            side,
            qty,
            token,
            price,
            f"{notional:,.2f}",
        )

        with self._lock:
            self.metrics.total_trades_count += 1
            if side == "BUY":
                self.metrics.session_buy_volume += notional
                self.token_quantity += qty
                self.total_token_quantity += qty
                self.cash = max(0.0, round(self.cash - notional, 2))
                self.frozen_cash = max(0.0, round(self.frozen_cash - notional, 2))
            else:
                self.metrics.session_sell_volume += notional
                self.token_quantity = max(0.0, round(self.token_quantity - qty, 1))
                self.total_token_quantity = max(0.0, round(self.total_token_quantity - qty, 1))
                self.cash = round(self.cash + notional, 2)

        # Trigger immediate next tick to recycle capital and quote the other side
        self.on_tick()

    def on_my_order(self, msg: Any) -> None:
        """Handles order lifecycle updates (FILLED, PARTIALLY_FILLED, CANCELLED)."""
        order_id = _get_field(msg, "orderId")
        status = str(_get_field(msg, "status", "")).upper()
        qty_left = _get_field(msg, "quantityLeft")

        if order_id in self.active_orders:
            tracker = self.active_orders[order_id]
            tracker.status = status
            if qty_left is not None:
                tracker.quantity_left = float(qty_left)

            if status in ("FILLED", "CANCELLED", "REJECTED"):
                self._remove_tracked_order(order_id)

    def on_balances(self, msg: Any) -> None:
        """Handles balance updates from private WebSocket stream."""
        cash = _get_field(msg, "cash")
        frozen = _get_field(msg, "frozen")
        with self._lock:
            if cash is not None:
                self.cash = float(cash)
            if frozen is not None:
                self.frozen_cash = float(frozen)

    def on_position(self, msg: Any) -> None:
        """Handles position updates from private WebSocket stream."""
        token_name = _get_field(msg, "tokenName")
        if token_name == self.token_name:
            qty = _get_field(msg, "quantity")
            total_qty = _get_field(msg, "totalQuantity")
            with self._lock:
                if qty is not None:
                    self.token_quantity = float(qty)
                if total_qty is not None:
                    self.total_token_quantity = float(total_qty)

    # ----------------------------------------------------------------------- #
    # Main Decision Loop (on_tick)
    # ----------------------------------------------------------------------- #

    def on_tick(self) -> None:
        """Strategy decision loop: calculates state, derives optimal quotes, and reconciles orders."""
        now = time.time()
        self._last_tick_time = now

        # 1. Periodic REST sync to catch fills and rebalance if private WS feed is unavailable
        if now - self._last_rest_sync >= self.config.rest_sync_interval:
            self._last_rest_sync = now
            self.sync_active_orders_from_rest()
            self.sync_portfolio_from_rest()

        # 2. Compute portfolio state and capital constraints
        state = self.calculate_portfolio_state()

        # 3. Derive optimal quotes based on top of book and inventory targets
        with self._lock:
            best_bid = self.best_bid
            best_ask = self.best_ask

        desired_buys, desired_sells = self.compute_quotes(
            best_bid=best_bid,
            best_ask=best_ask,
            ref_price=state["ref_price"],
            available_cash=state["available_cash"],
            available_tokens=state["available_tokens"],
            target_budget=state["target_budget"],
            current_token_val=state["token_value"],
        )

        # 4. Reconcile resting orders against desired quotes
        self.reconcile_orders(desired_buys, desired_sells)

        # 5. Periodic status display
        if now - self._last_status_print >= 10.0:
            self._last_status_print = now
            self.print_status(state)

    def print_status(self, state: dict[str, float] | None = None) -> None:
        """Prints a clean, informative terminal status snapshot."""
        if state is None:
            state = self.calculate_portfolio_state()

        with self._lock:
            bid = self.best_bid
            ask = self.best_ask
            mark = self.mark_price
            m = self.metrics
            free_cash = self.cash
            frozen_cash = self.frozen_cash
            free_tokens = self.token_quantity
            total_tokens = state.get("total_tokens", self.token_quantity)

        spread_str = f"${ask - bid:,.2f}" if (bid and ask) else "N/A"
        active_buys_count = len(self.active_buy_orders)
        active_sells_count = len(self.active_sell_orders)

        print("\n" + "=" * 72)
        print(f"📊 LOAF VOLUME MAXIMIZER | Token: [{self.token_name.upper()}]")
        print(f"   Market Book: Bid=${bid} | Ask=${ask} | Spread={spread_str} | Mark=${mark}")
        print(
            f"   Portfolio:   Equity=${state['total_equity']:,.2f} "
            f"(Target 70% Budget: ${state['target_budget']:,.2f} | Reserve 30%: ${state['reserve_budget']:,.2f})"
        )
        print(
            f"   Holdings:    Free Cash=${free_cash:,.2f} USDL (Frozen: ${frozen_cash:,.2f}) | "
            f"Tokens={total_tokens:.1f} (Free: {free_tokens:.1f}, Val: ${state['token_value']:,.2f}) | "
            f"Utilization: {state['utilization_pct'] * 100.0:.1f}% / 70.0%"
        )
        print(
            f"   Active Orders: {active_buys_count} BUYs, {active_sells_count} SELLs | "
            f"Total Placed: {m.orders_placed_count} | Fills: {m.total_trades_count}"
        )
        print(
            f"   🔥 VOLUME:   Session=${m.total_session_volume:,.2f} "
            f"(Buy=${m.session_buy_volume:,.2f} + Sell=${m.session_sell_volume:,.2f}) | "
            f"Lifetime=${m.lifetime_volume:,.2f}"
        )
        print(f"   PnL:         Session=${m.session_pnl:+,.2f} ({m.session_pnl_pct:+.2f}%)")
        print("=" * 72 + "\n")

