"""Competition leaderboard (public)."""

from __future__ import annotations

from typing import Any

from .base import Resource


class LeaderboardResource(Resource):
    def get(self) -> Any:
        """``GET /leaderboard`` — the current competition leaderboard. (public)

        Returns ``{roundNumber, roundName, roundRules, roundStatus,
        volumeMultiplierTiers, entries, newAssetProperty}`` where each entry has
        ``rank``, ``handle``, ``walletAddress``, ``points``, ``volume``, ``pnl``
        (volume/pnl in whole USDC, rounded to cents). Serves a live board while
        a round is ``ACTIVE`` or the last round's frozen snapshot between
        rounds. Raises 404 when no round/snapshot exists.

        For live updates, prefer subscribing to the ``leaderboard`` WebSocket
        channel (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_leaderboard`)
        over polling — the server pushes a ``leaderboard_update`` whenever the
        board changes.
        """
        return self._client.get("/leaderboard", auth=False)
