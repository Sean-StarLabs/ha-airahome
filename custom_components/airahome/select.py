"""Pending hot water boost duration for Aira Heat Pump."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_DEVICE_UUID, DOMAIN
from .controls import AiraPendingControls, control_device_info
from .services import DHW_BOOST_VALID_HOURS


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the local boost duration selection."""
    async_add_entities([AiraBoostDuration(
        entry, hass.data[DOMAIN][entry.entry_id]["pending_controls"],
    )])


class AiraBoostDuration(RestoreEntity, SelectEntity):
    """A duration selection applied only by the Start Boost button."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "dhw_boost_duration"
    _attr_icon = "mdi:timer-outline"

    def __init__(self, entry: ConfigEntry, pending: AiraPendingControls) -> None:
        self._attr_options = [str(hours) for hours in DHW_BOOST_VALID_HOURS]
        self._pending = pending
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_UUID]}_dhw_boost_duration"
        self._attr_device_info = control_device_info(entry)

    @property
    def current_option(self) -> str:
        """Return the pending duration in hours."""
        return self._pending.dhw_boost_duration

    async def async_added_to_hass(self) -> None:
        """Restore a selection without starting a boost."""
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) and state.state in self.options:
            self._pending.dhw_boost_duration = state.state

    async def async_select_option(self, option: str) -> None:
        """Store a valid duration locally."""
        if option not in self.options:
            raise ServiceValidationError(f"Unsupported boost duration: {option}")
        self._pending.dhw_boost_duration = option
        self.async_write_ha_state()
