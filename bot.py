#!/usr/bin/env python3
"""Loaf Volume Maximizing Trading Bot.

A production-ready trading bot for Loaf Markets designed to:
- Deploy 70% of total portfolio equity (maintaining a 30% risk reserve).
- Maximize trading volume via tight two-sided limit quoting (spread capture and price improvement).
- Balance inventory continuously (USDL <-> Tokens) to maximize sustainable turnover.
- Cleanly handle platform halts, competition eligibility, nonces, and order lifecycles.

Usage:
    # Set up credentials in .env or environment:
    export LOAF_API_KEY="your-api-key"
    export LOAF_TARGET_TOKEN="opera"
    export LOAF_USER_ID="123"

    # Run live trading (70% portfolio allocation by default):
    python bot.py

    # Run in simulation / dry-run mode:
    python bot.py --dry-run

    # Emergency cancel all open orders:
    python bot.py --panic
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

import loaf
from loaf import LoafClient, StrategyConfig, VolumeMaximizerStrategy

# Optionally load environment variables from .env
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("loaf.bot")


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments for the trading bot."""
    parser = argparse.ArgumentParser(
        description="Loaf Volume Maximizing Trading Bot (70% Portfolio Utilization)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("LOAF_TARGET_TOKEN", ""),
        help="Target property tokenName (e.g. 'opera'). Defaults to first listed if unset.",
    )
    parser.add_argument(
        "--portfolio-pct",
        type=float,
        default=float(os.environ.get("LOAF_PORTFOLIO_PCT", "0.70")),
        help="Fraction of portfolio to utilize for active trading (0.70 = 70%%).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Decision tick interval in seconds.",
    )
    parser.add_argument(
        "--spread-bps",
        type=float,
        default=20.0,
        help="Spread offset in basis points (20 bps = 0.20%%) for empty/one-sided books.",
    )
    parser.add_argument(
        "--tranches",
        type=int,
        default=1,
        help="Number of order tranches/slices per side.",
    )
    parser.add_argument(
        "--stale-timeout",
        type=float,
        default=20.0,
        help="Timeout in seconds to refresh unfilled resting orders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate quoting calculations without placing real exchange orders.",
    )
    parser.add_argument(
        "--panic",
        "--cancel-all",
        action="store_true",
        dest="panic",
        help="Emergency flatten: cancel all open orders on the exchange and exit.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print current market info and portfolio snapshot, then exit.",
    )
    return parser.parse_args()


def build_client() -> LoafClient:
    """Builds and returns a LoafClient instance configured from environment."""
    api_key = os.environ.get("LOAF_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: LOAF_API_KEY is not set.\n"
            "  1. Log in to https://beta.loafmarkets.com\n"
            "  2. Mint an API key under Settings -> API keys (https://beta.loafmarkets.com/api)\n"
            "  3. Set LOAF_API_KEY in your .env or environment.\n"
        )
    return LoafClient(api_key=api_key)


def preflight(client: LoafClient) -> Any:
    """Performs connection, auth, and portfolio preflight checks."""
    try:
        comp = client.portfolio.component()
    except loaf.LoafAuthError as exc:
        sys.exit(f"Authentication failed: Check LOAF_API_KEY. ({exc.message})")
    except Exception as exc:
        sys.exit(f"Failed to connect to Loaf API ({client.base_url}): {exc}")

    print("\n" + "=" * 72)
    print("🚀 LOAF TRADING BOT - INITIALIZING")
    print("=" * 72)
    print(f"  Connected Base URL: {client.base_url}")
    print(f"  Available Cash:     ${comp.cash:,.2f} USDL")
    print(f"  Frozen in Orders:   ${comp.frozen:,.2f} USDL")
    print(f"  Portfolio Equity:   ${comp.portfolioValue:,.2f} USDL")
    print(f"  Lifetime Volume:    ${getattr(comp, 'lifetimeVolume', 0.0):,.2f} USDL")

    positions = comp.get("positions") or []
    if positions:
        print("  Current Holdings:")
        for p in positions:
            print(
                f"    - {p.tokenName}: {p.quantity} tokens @ avg entry ${p.averageEntryPrice:,.2f} "
                f"(mkt ${p.marketPrice:,.2f}, PnL ${p.propertyPnl:+,.2f})"
            )
    else:
        print("  Current Holdings:   None (All Cash)")

    # Check competition status
    try:
        queue = client.competition.queue_position()
        if queue.get("activeRound"):
            print(f"  Competition Status: Active Round - Standing: {queue.get('position', 'Admitted')}")
    except Exception:
        pass

    print("=" * 72 + "\n")
    return comp


def resolve_target_property(client: LoafClient, target_name: str) -> Any:
    """Resolves target token from the market property listings."""
    listing = client.market.properties().get("properties") or []
    if not listing:
        sys.exit("No tradeable properties found on this Loaf environment.")

    if target_name:
        for prop in listing:
            if prop.tokenName.lower() == target_name.lower():
                return prop
        print(f"Warning: Target token '{target_name}' not found. Defaulting to '{listing[0].tokenName}'.")

    return listing[0]


def main() -> None:
    args = parse_args()
    client = build_client()

    # Emergency cancel mode
    if args.panic:
        print("Executing panic flatten: cancelling all open orders...")
        try:
            res = client.orders.cancel_all()
            print(f"Cancelled orders: {res.get('cancelledOrderIds', [])}")
        except Exception as e:
            print(f"Cancel all returned: {e}")
        client.close()
        return

    # Preflight
    comp = preflight(client)
    target_prop = resolve_target_property(client, args.token)
    token_name = target_prop.tokenName

    # If --info requested, print overview and exit
    if args.info:
        detail = client.market.property(token_name)
        book = detail.get("orderBook")
        best_bid = book.bids[0].price if (book and book.get("bids")) else None
        best_ask = book.asks[0].price if (book and book.get("asks")) else None
        print(f"Property Info: {token_name} (ID: {target_prop.propertyId})")
        print(f"  Market Price:  ${target_prop.marketPrice}")
        print(f"  24h Volume:    ${target_prop.volume24h:,.2f}")
        print(f"  Best Bid:      ${best_bid}")
        print(f"  Best Ask:      ${best_ask}")
        client.close()
        return

    # Pre-approve allowances to optimize execution latency
    if not args.dry_run:
        try:
            client.orders.approve(token_name)
            logger.info("Pre-approved trading allowances for %s", token_name)
        except Exception as e:
            logger.debug("Pre-approval skipped or unnecessary: %s", e)

    # Determine fallback market price from listings to avoid initial mispricing
    fallback_price = float(target_prop.marketPrice) if getattr(target_prop, "marketPrice", None) is not None else None

    # Instantiate strategy
    config = StrategyConfig(
        target_token=token_name,
        portfolio_pct=args.portfolio_pct,
        fallback_price=fallback_price,
        spread_bps=args.spread_bps,
        order_tranches=args.tranches,
        tick_interval=args.interval,
        stale_order_timeout=args.stale_timeout,
        dry_run=args.dry_run,
    )

    strategy = VolumeMaximizerStrategy(client, config)

    # Seed state from REST
    strategy.sync_portfolio_from_rest()
    strategy.sync_active_orders_from_rest()

    # Configure WebSocket feeds
    ws = client.websocket()
    ws.on_orderbook(strategy.on_orderbook)
    ws.on_mark_price(strategy.on_mark_price)
    ws.on_trades(strategy.on_trade_tick)
    ws.on_trade(strategy.on_my_fill)  # private: fills
    ws.on_order_status(strategy.on_my_order)  # private: order lifecycle
    ws.on_balances(strategy.on_balances)  # private: balances
    ws.on_position(strategy.on_position)  # private: positions
    ws.on_error(lambda m: logger.warning("WebSocket warning: %s", m.get("message")))

    # Subscribe to market data channels
    ws.subscribe_orderbook(token_name)
    ws.subscribe_mark_price(token_name)
    ws.subscribe_trades(token_name)

    # Subscribe to private portfolio channel if user ID is configured or discoverable
    user_id = os.environ.get("LOAF_USER_ID")
    if not user_id and hasattr(comp, "userId") and comp.userId:
        user_id = str(comp.userId)
    elif not user_id and isinstance(comp, dict) and comp.get("userId"):
        user_id = str(comp.get("userId"))

    if user_id:
        try:
            ws.subscribe_portfolio(int(user_id))
            logger.info("Subscribed to private portfolio channel for user %s", user_id)
        except ValueError:
            logger.warning("Invalid LOAF_USER_ID '%s' — private portfolio WS stream disabled.", user_id)
    else:
        logger.info("LOAF_USER_ID unset — streaming public book; private fills synced via REST.")

    # Start WebSocket background connection
    ws.start()
    ws.wait_until_connected(timeout=10)
    logger.info("WebSocket connected. Starting volume-maximizing loop (allocation: %.0f%%).", config.portfolio_pct * 100)

    # Graceful shutdown handler
    stop_event = threading.Event()

    def handle_signal(*_: Any) -> None:
        logger.info("\nShutdown signal received. Cleaning up...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop_event.is_set():
            strategy.on_tick()
            stop_event.wait(config.tick_interval)
    finally:
        logger.info("Cancelling resting orders and stopping feeds...")
        strategy.cancel_all_orders()
        ws.stop()
        client.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
