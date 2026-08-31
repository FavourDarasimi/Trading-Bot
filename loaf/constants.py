"""Protocol constants for the Loaf API.

These capture the wire-level contract every client must honour: numeric
precision limits, order sentinels, and pagination bounds.
"""

from __future__ import annotations

from ._version import __version__

# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

#: Default REST base URL — the production Loaf API (every route is mounted under
#: ``/api``). For local development against a dev server, set ``LOAF_API_BASE_URL``
#: (or pass ``base_url=``), e.g. ``http://localhost:8005/api``.
DEFAULT_BASE_URL = "https://api.loafmarkets.com/api"

#: Environment variables the client reads when arguments are omitted.
ENV_API_KEY = "LOAF_API_KEY"
ENV_BASE_URL = "LOAF_API_BASE_URL"
ENV_WS_URL = "LOAF_WS_URL"

#: User-Agent sent on every request.
USER_AGENT = f"loaf-python-sdk/{__version__}"

#: Default per-request timeout (seconds).
DEFAULT_TIMEOUT = 30.0

#: Default number of automatic retries for transient failures (429/503/network).
DEFAULT_MAX_RETRIES = 3

# --------------------------------------------------------------------------- #
# Numeric precision
# --------------------------------------------------------------------------- #

#: Max decimal places accepted for a limit price (cents). Enforced server-side.
MAX_PRICE_DECIMALS = 2

#: Max decimal places accepted for a token quantity (0.1 of a token).
MAX_QUANTITY_DECIMALS = 1

# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #

#: A MARKET order MUST send ``price = 0``.
MARKET_ORDER_PRICE = 0

#: Non-GTD orders MUST send ``deadline = 0``.
DEFAULT_ORDER_DEADLINE = 0
