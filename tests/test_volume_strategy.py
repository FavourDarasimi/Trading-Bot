"""Comprehensive unit and scenario tests for the Volume Maximizer Strategy."""

from __future__ import annotations

import httpx
import pytest

import loaf
from loaf import LoafClient, StrategyConfig, VolumeMaximizerStrategy
from loaf.enums import OrderSide
from loaf.exceptions import LoafValidationError
from loaf.money import validate_price, validate_quantity

BASE = "http://test/api"


def make_test_client(handler=None, **kwargs) -> LoafClient:
    """Creates a mock client for testing without external network calls."""
    if handler is None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=BASE)
    return LoafClient(api_key="test_key", base_url=BASE, http_client=http, **kwargs)


# --------------------------------------------------------------------------- #
# 1. 70% Portfolio Allocation Tests
# --------------------------------------------------------------------------- #


def test_portfolio_allocation_all_cash():
    """When portfolio is 100% cash, 70% is available for buying and 30% is reserve."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70)
    strategy = VolumeMaximizerStrategy(client, config)

    strategy.cash = 1000.0
    strategy.frozen_cash = 0.0
    strategy.token_quantity = 0.0
    strategy.mark_price = 100.0

    state = strategy.calculate_portfolio_state()

    assert state["total_equity"] == 1000.0
    assert state["target_budget"] == 700.0  # 70% of 1000
    assert state["reserve_budget"] == 300.0  # 30% of 1000
    assert state["available_cash"] == 700.0
    assert state["available_tokens"] == 0.0
    assert state["currently_committed"] == 0.0


def test_portfolio_allocation_mixed_inventory():
    """When portfolio holds cash and tokens, available cash respects the 70% ceiling."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70)
    strategy = VolumeMaximizerStrategy(client, config)

    # 500 USDL cash, 5 tokens @ $100 = $500 token value -> Total Equity = $1000
    strategy.cash = 500.0
    strategy.frozen_cash = 0.0
    strategy.token_quantity = 5.0
    strategy.mark_price = 100.0

    state = strategy.calculate_portfolio_state()

    assert state["total_equity"] == 1000.0
    assert state["target_budget"] == 700.0
    assert state["currently_committed"] == 500.0  # $500 in tokens
    # Max new capital that can be deployed = 700 - 500 = 200 USDL
    assert state["available_cash"] == 200.0
    assert state["available_tokens"] == 5.0


def test_portfolio_allocation_over_utilized_cap():
    """If current positions already equal or exceed 70%, no additional cash is deployed."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70)
    strategy = VolumeMaximizerStrategy(client, config)

    # 200 USDL cash, 8 tokens @ $100 = $800 token value -> Total Equity = $1000
    # Committed = 800 (80% of portfolio > 70%)
    strategy.cash = 200.0
    strategy.frozen_cash = 0.0
    strategy.token_quantity = 8.0
    strategy.mark_price = 100.0

    state = strategy.calculate_portfolio_state()

    assert state["total_equity"] == 1000.0
    assert state["target_budget"] == 700.0
    assert state["currently_committed"] == 800.0
    # Available cash for new buys is clamped to 0.0
    assert state["available_cash"] == 0.0
    assert state["available_tokens"] == 8.0


# --------------------------------------------------------------------------- #
# 2. Quote Calculation & Order Sizing Tests
# --------------------------------------------------------------------------- #


def test_compute_quotes_wide_spread():
    """With wide spread, bot quotes inside the spread (+0.01 / -0.01) to capture volume."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, min_tick=0.01)
    strategy = VolumeMaximizerStrategy(client, config)

    best_bid = 120.00
    best_ask = 125.00  # Spread = $5.00
    ref_price = 122.50
    available_cash = 700.0
    available_tokens = 5.0
    target_budget = 700.0
    current_token_val = 612.50

    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=best_bid,
        best_ask=best_ask,
        ref_price=ref_price,
        available_cash=available_cash,
        available_tokens=available_tokens,
        target_budget=target_budget,
        current_token_val=current_token_val,
    )

    assert len(buy_quotes) >= 1
    assert len(sell_quotes) >= 1

    buy_p, buy_q = buy_quotes[0]
    sell_p, sell_q = sell_quotes[0]

    # Inside spread: best_bid + 0.01 = 120.01, best_ask - 0.01 = 124.99
    assert buy_p == 120.01
    assert sell_p == 124.99
    assert buy_p < sell_p  # Invariant: buy < sell

    # Validate exchange precision constraints
    validate_price(buy_p)
    validate_price(sell_p)
    validate_quantity(buy_q)
    validate_quantity(sell_q)

    # Minimum notional check (>= 10 USDL)
    assert (buy_p * buy_q) >= 10.0
    assert (sell_p * sell_q) >= 10.0


def test_compute_quotes_tight_spread():
    """With tight spread ($0.01), bot joins top of book on bid and ask."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, min_tick=0.01)
    strategy = VolumeMaximizerStrategy(client, config)

    best_bid = 100.00
    best_ask = 100.01
    ref_price = 100.005

    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=best_bid,
        best_ask=best_ask,
        ref_price=ref_price,
        available_cash=350.0,
        available_tokens=3.5,
        target_budget=700.0,
        current_token_val=350.0,
    )

    assert len(buy_quotes) == 1
    assert len(sell_quotes) == 1
    assert buy_quotes[0][0] == 100.00
    assert sell_quotes[0][0] == 100.01
    assert buy_quotes[0][0] < sell_quotes[0][0]


def test_compute_quotes_empty_book():
    """When order book is empty, bot quotes around mark price with configured spread offset."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, spread_bps=50.0)  # 0.50%
    strategy = VolumeMaximizerStrategy(client, config)

    mark_price = 200.00

    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=None,
        best_ask=None,
        ref_price=mark_price,
        available_cash=500.0,
        available_tokens=2.5,
        target_budget=700.0,
        current_token_val=500.0,
    )

    assert len(buy_quotes) == 1
    assert len(sell_quotes) == 1

    buy_p, _ = buy_quotes[0]
    sell_p, _ = sell_quotes[0]

    # 0.50% offset of 200 = $1.00 -> buy at 199.00, sell at 201.00
    assert buy_p == 199.00
    assert sell_p == 201.00
    assert buy_p < sell_p


def test_strict_self_crossing_prevention():
    """Ensures that under no circumstances can buy_price >= sell_price."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", min_tick=0.01)
    strategy = VolumeMaximizerStrategy(client, config)

    # Edge case: crossed input book
    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=100.00,
        best_ask=99.00,  # crossed book
        ref_price=100.00,
        available_cash=200.0,
        available_tokens=2.0,
        target_budget=500.0,
        current_token_val=200.0,
    )

    if buy_quotes and sell_quotes:
        assert buy_quotes[0][0] < sell_quotes[0][0]


# --------------------------------------------------------------------------- #
# 3. Precision and Quantity Constraints Tests
# --------------------------------------------------------------------------- #


def test_order_sizing_precision_rules():
    """Quantity must have at most 1 decimal place; price at most 2 decimal places."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70)
    strategy = VolumeMaximizerStrategy(client, config)

    # Test with fractional numbers that would result in multi-decimal values
    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=133.33,
        best_ask=137.77,
        ref_price=135.55,
        available_cash=312.45,
        available_tokens=2.3456,
        target_budget=700.0,
        current_token_val=317.93,
    )

    for p, q in buy_quotes + sell_quotes:
        validate_price(p)
        validate_quantity(q)
        # Verify 1 decimal place for quantity
        assert round(q, 1) == q
        # Verify 2 decimal places for price
        assert round(p, 2) == p


def test_minimum_notional_enforced():
    """Orders with notional below 10 USDL must not be placed unless closing whole position."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", min_order_notional=10.0)
    strategy = VolumeMaximizerStrategy(client, config)

    # Only $5 USDL available (below $10 min notional)
    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=100.00,
        best_ask=105.00,
        ref_price=102.50,
        available_cash=5.0,
        available_tokens=0.0,
        target_budget=10.0,
        current_token_val=0.0,
    )

    assert len(buy_quotes) == 0  # Cannot place BUY under $10


# --------------------------------------------------------------------------- #
# 4. Fill Processing & Volume Metrics Tests
# --------------------------------------------------------------------------- #


def test_fill_processing_and_volume_accumulation():
    """When a fill arrives, volume statistics update and trigger capital recycling."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70)
    strategy = VolumeMaximizerStrategy(client, config)

    strategy.cash = 1000.0
    strategy.token_quantity = 0.0

    # 1. Simulate BUY fill of 2 tokens @ $100 ($200 notional)
    buy_fill = {
        "trade": {
            "side": "BUY",
            "quantity": 2.0,
            "tokenName": "opera",
            "price": 100.00,
            "fee": 0.20,
        }
    }
    strategy.on_my_fill(buy_fill)

    assert strategy.metrics.session_buy_volume == 200.0
    assert strategy.metrics.session_sell_volume == 0.0
    assert strategy.metrics.total_session_volume == 200.0
    assert strategy.metrics.total_trades_count == 1
    assert strategy.token_quantity == 2.0
    assert strategy.cash == 800.0

    # 2. Simulate SELL fill of 2 tokens @ $105 ($210 notional)
    sell_fill = {
        "trade": {
            "side": "SELL",
            "quantity": 2.0,
            "tokenName": "opera",
            "price": 105.00,
            "fee": 0.21,
        }
    }
    strategy.on_my_fill(sell_fill)

    assert strategy.metrics.session_buy_volume == 200.0
    assert strategy.metrics.session_sell_volume == 210.0
    assert strategy.metrics.total_session_volume == 410.0
    assert strategy.metrics.total_trades_count == 2
    assert strategy.token_quantity == 0.0
    assert strategy.cash == 1010.0


# --------------------------------------------------------------------------- #
# 5. Order Tracking & Reconciliation Tests
# --------------------------------------------------------------------------- #


def test_dry_run_order_placement_and_reconciliation():
    """In dry-run mode, orders are tracked in simulation without calling exchange."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, dry_run=True)
    strategy = VolumeMaximizerStrategy(client, config)

    strategy.cash = 700.0
    strategy.token_quantity = 5.0
    strategy.best_bid = 100.00
    strategy.best_ask = 105.00
    strategy.mark_price = 102.50

    strategy.on_tick()

    assert strategy.metrics.orders_placed_count >= 1
    assert len(strategy.active_orders) >= 1
    assert len(strategy.active_buy_orders) >= 1
    assert len(strategy.active_sell_orders) >= 1

    # Emergency flatten
    strategy.cancel_all_orders()
    assert len(strategy.active_orders) == 0
    assert len(strategy.active_buy_orders) == 0
    assert len(strategy.active_sell_orders) == 0


# --------------------------------------------------------------------------- #
# 6. Inventory Rebalancing & Edge Case Tests
# --------------------------------------------------------------------------- #


def test_inventory_skew_rebalancing():
    """When token inventory is heavily skewed (> 15%), selling is prioritized over buying."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, inventory_skew_threshold=0.15)
    strategy = VolumeMaximizerStrategy(client, config)

    # Total budget = 700 USDL. Target half = 350.
    # Current token value = 600 USDL (heavy overweight +250/700 = +35.7% > 15%)
    buy_quotes, sell_quotes = strategy.compute_quotes(
        best_bid=100.0,
        best_ask=105.0,
        ref_price=100.0,
        available_cash=100.0,
        available_tokens=6.0,
        target_budget=700.0,
        current_token_val=600.0,
    )

    # Sells are placed to liquidate overweight inventory
    assert len(sell_quotes) >= 1
    assert sell_quotes[0][1] == 6.0


def test_stale_order_price_drift_cancellation():
    """Resting orders are cancelled if market price drifts beyond threshold."""
    client = make_test_client()
    config = StrategyConfig(
        target_token="opera",
        portfolio_pct=0.70,
        price_deviation_threshold=0.01,  # 1% drift
        dry_run=True,
    )
    strategy = VolumeMaximizerStrategy(client, config)

    # Place initial simulated BUY @ $100.00
    strategy._place_order(OrderSide.BUY, price=100.00, quantity=1.0)
    assert len(strategy.active_buy_orders) == 1

    # Desired new price has drifted to $105.00 (> 1% drift)
    desired_buys = [(105.00, 1.0)]
    desired_sells = []

    strategy.reconcile_orders(desired_buys, desired_sells)

    # Old order cancelled, new order placed at $105.00
    assert len(strategy.active_buy_orders) == 1
    tracker = list(strategy.active_buy_orders.values())[0]
    assert tracker.price == 105.00


def test_websocket_event_handlers():
    """WebSocket callbacks update internal state correctly."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", dry_run=True)
    strategy = VolumeMaximizerStrategy(client, config)

    # Orderbook
    strategy.on_orderbook({"bids": [{"price": 110.5}], "asks": [{"price": 112.5}]})
    assert strategy.best_bid == 110.5
    assert strategy.best_ask == 112.5
    assert strategy.get_reference_price() == 111.5

    # Mark price
    strategy.on_mark_price({"price": 111.8})
    assert strategy.mark_price == 111.8

    # Public trade
    strategy.on_trade_tick({"trades": [{"price": 111.2}]})
    assert strategy.last_trade_price == 111.2

    # Balances
    strategy.on_balances({"cash": 2500.0, "frozen": 150.0})
    assert strategy.cash == 2500.0
    assert strategy.frozen_cash == 150.0

    # Position
    strategy.on_position({"tokenName": "opera", "quantity": 12.5})
    assert strategy.token_quantity == 12.5

    # Order lifecycle update
    strategy._place_order(OrderSide.BUY, price=100.0, quantity=1.0)
    sim_id = list(strategy.active_orders.keys())[0]
    strategy.on_my_order({"orderId": sim_id, "status": "FILLED", "quantityLeft": 0.0})
    assert sim_id not in strategy.active_orders


def test_resting_buy_order_retained_across_ticks():
    """Ensures an active resting BUY order is not cancelled on subsequent ticks when funds are frozen."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, tick_interval=2.0, stale_order_timeout=20.0, dry_run=True)
    strategy = VolumeMaximizerStrategy(client, config)

    strategy.cash = 1000.0
    strategy.frozen_cash = 0.0
    strategy.token_quantity = 0.0
    strategy.best_bid = 100.00
    strategy.best_ask = 101.00
    strategy.mark_price = 100.50

    # 1. First tick: places initial BUY order @ $100.01 ($690.07 notional)
    strategy.on_tick()
    assert len(strategy.active_buy_orders) == 1
    order_id = list(strategy.active_buy_orders.keys())[0]
    assert strategy.active_buy_orders[order_id].quantity == 6.9
    assert strategy.active_buy_orders[order_id].price == 100.01
    assert strategy.cash == 309.93
    assert strategy.frozen_cash == 690.07

    # 2. Second tick: market book unchanged, order age < 20s.
    # The resting order MUST remain active and NOT be cancelled!
    strategy.on_tick()
    assert len(strategy.active_buy_orders) == 1
    assert order_id in strategy.active_buy_orders
    assert strategy.metrics.orders_cancelled_count == 0


def test_resting_sell_order_retained_across_ticks():
    """Ensures an active resting SELL order is not cancelled on subsequent ticks when tokens are frozen."""
    client = make_test_client()
    config = StrategyConfig(target_token="opera", portfolio_pct=0.70, tick_interval=2.0, stale_order_timeout=20.0, dry_run=True)
    strategy = VolumeMaximizerStrategy(client, config)

    # 1000 USDL equity: $300 cash, 7 tokens @ $100
    strategy.cash = 300.0
    strategy.frozen_cash = 0.0
    strategy.token_quantity = 7.0
    strategy.best_bid = 100.00
    strategy.best_ask = 101.00
    strategy.mark_price = 100.50

    # 1. First tick: places SELL order for 7 tokens @ $101.00
    strategy.on_tick()
    assert len(strategy.active_sell_orders) == 1
    order_id = list(strategy.active_sell_orders.keys())[0]
    assert strategy.active_sell_orders[order_id].quantity == 7.0
    assert strategy.token_quantity == 0.0  # Tokens now locked in active sell order

    # 2. Second tick: market book unchanged, order age < 20s.
    # The resting order MUST remain active and NOT be cancelled!
    strategy.on_tick()
    assert len(strategy.active_sell_orders) == 1
    assert order_id in strategy.active_sell_orders
    assert strategy.metrics.orders_cancelled_count == 0


