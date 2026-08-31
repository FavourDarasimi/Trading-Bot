"""Base class shared by all resource groups."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import LoafClient


class Resource:
    """Holds a back-reference to the owning client."""

    def __init__(self, client: LoafClient) -> None:
        self._client = client
