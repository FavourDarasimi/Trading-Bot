"""05 — Stream your PRIVATE portfolio events (requires an API key + user id).

Receives balance changes, position updates, order transitions, your fills,
transfers, and IPO order updates in real time.

    LOAF_USER_ID=<your numeric user id> python examples/05_portfolio_stream.py

Your numeric user id is shown in the Loaf web app.
"""

from __future__ import annotations

import os

from loaf import LoafClient

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    user_id = os.environ.get("LOAF_USER_ID")
    if not user_id:
        raise SystemExit("Set LOAF_USER_ID to your numeric Loaf user id (see the web app).")

    client = LoafClient()  # needs LOAF_API_KEY for the private channel

    ws = client.websocket()

    ws.on_balances(lambda m: print(f"BALANCE cash={m.cash:,.2f} frozen={m.frozen:,.2f}"))
    ws.on_position(lambda m: print(f"POSITION {m.tokenName} qty={m.quantity} "
                                   f"total={m.totalQuantity} avg={m.averageEntryPrice}"))
    ws.on_order_status(lambda m: print(f"ORDER #{m.orderId} -> {m.status} "
                                       f"(left {m.get('quantityLeft')})"))
    ws.on_trade(lambda m: print(f"FILL {m.trade.side} {m.trade.quantity} "
                                f"{m.trade.tokenName} @ {m.trade.price}"))
    ws.on_transfer(lambda m: print(f"TRANSFER {m.transfer.type} {m.transfer.amount} "
                                   f"-> {m.transfer.status}"))
    ws.on_offering_order(lambda m: print(f"IPO ORDER #{m.order.ipoOrderId} -> {m.order.status}"))
    ws.on_error(lambda m: print(f"ERROR {m.get('message')}"))

    # Subscribe to your own private channel.
    ws.subscribe_portfolio(int(user_id))
    print("Listening for portfolio events. Trade in another window to see updates. "
          "Ctrl-C to stop.\n")

    ws.run_forever()


if __name__ == "__main__":
    main()
