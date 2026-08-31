"""Loaf API SDK — a Python client + bot template for the Loaf trading platform.

Quick start::

    from loaf import LoafClient

    loaf = LoafClient(api_key="your-api-key")     # or set $LOAF_API_KEY
    print(loaf.portfolio.component().cash)
    print(loaf.market.properties())

See the README for the full guide.
"""

from __future__ import annotations

from ._object import LoafObject
from ._version import __version__
from .client import LoafClient
from .constants import (
    DEFAULT_BASE_URL,
    MARKET_ORDER_PRICE,
    MAX_PRICE_DECIMALS,
    MAX_QUANTITY_DECIMALS,
)
from .enums import (
    CandleResolution,
    CompetitionRoundStatus,
    IpoStatus,
    OfferingOrderStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PropertyStatus,
    TimeInForce,
    TradeStatus,
    TransferStatus,
    TransferType,
    WSMessageType,
)
from .exceptions import (
    CompetitionEligibilityError,
    KycRequiredError,
    LoafAPIError,
    LoafAuthError,
    LoafBusinessRuleError,
    LoafConfigError,
    LoafConflictError,
    LoafConnectionError,
    LoafError,
    LoafForbiddenError,
    LoafNotFoundError,
    LoafRateLimitError,
    LoafServerError,
    LoafServiceUnavailableError,
    LoafValidationError,
    TradingHaltedError,
)
from .money import bps_to_fraction, fraction_to_bps
from .strategy import StrategyConfig, StrategyMetrics, VolumeMaximizerStrategy
from .ws import LoafWebSocketClient

__all__ = [
    "__version__",
    # strategy
    "VolumeMaximizerStrategy",
    "StrategyConfig",
    "StrategyMetrics",
    # client
    "LoafClient",
    "LoafWebSocketClient",
    "LoafObject",
    # constants
    "DEFAULT_BASE_URL",
    "MARKET_ORDER_PRICE",
    "MAX_PRICE_DECIMALS",
    "MAX_QUANTITY_DECIMALS",
    # enums
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "OrderStatus",
    "TradeStatus",
    "OfferingOrderStatus",
    "PropertyStatus",
    "IpoStatus",
    "TransferType",
    "TransferStatus",
    "CandleResolution",
    "CompetitionRoundStatus",
    "WSMessageType",
    # unit helpers
    "bps_to_fraction",
    "fraction_to_bps",
    # exceptions
    "LoafError",
    "LoafConfigError",
    "LoafConnectionError",
    "LoafAPIError",
    "LoafAuthError",
    "LoafForbiddenError",
    "KycRequiredError",
    "TradingHaltedError",
    "CompetitionEligibilityError",
    "LoafValidationError",
    "LoafNotFoundError",
    "LoafConflictError",
    "LoafBusinessRuleError",
    "LoafRateLimitError",
    "LoafServerError",
    "LoafServiceUnavailableError",
]
