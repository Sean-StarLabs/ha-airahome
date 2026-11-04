"""Availability follows the response a particular entity depends on."""

from __future__ import annotations


class AiraChannelEntity:
    """Mix into coordinator entities; diagnostics must not outlive their evidence."""

    _aira_channel = "state"

    @property
    def available(self) -> bool:
        path = getattr(self, "_data_path", ())
        if path and path[0] == "connected":
            return not self.coordinator._closing
        channel = (
            path[0]
            if path and path[0] in ("state", "system_check_state")
            else self._aira_channel
        )
        return super().available and self.coordinator.channel_available(channel)
