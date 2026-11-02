"""Pending Away dates for Aira Heat Pump."""
from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_DEVICE_UUID, DOMAIN
from .controls import AiraPendingControls, control_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the local Away date selections."""
    pending = hass.data[DOMAIN][entry.entry_id]["pending_controls"]
    async_add_entities([
        AiraAwayDate(entry, pending, "away_start_date"),
        AiraAwayDate(entry, pending, "away_end_date"),
    ])


class AiraAwayDate(RestoreEntity, DateEntity):
    """A local date selection, not the heat pump's current Away schedule."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:calendar"

    def __init__(self, entry: ConfigEntry, pending: AiraPendingControls, key: str) -> None:
        self._pending = pending
        self._key = key
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_UUID]}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = control_device_info(entry)

    @property
    def native_value(self) -> date:
        """Return the pending date."""
        return getattr(self._pending, self._key)

    async def async_added_to_hass(self) -> None:
        """Restore a selection without sending a device command."""
        await super().async_added_to_hass()
        if state := await self.async_get_last_state():
            try:
                value = date.fromisoformat(state.state)
            except ValueError:
                return
            setattr(self._pending, self._key, value)

    async def async_set_value(self, value: date) -> None:
        """Store a selection; the Activate Away button validates the date range."""
        setattr(self._pending, self._key, value)
        self.async_write_ha_state()
