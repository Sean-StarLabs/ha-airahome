"""Explicit Away, boost and night mode actions for Aira Heat Pump."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_UUID, DOMAIN
from .controls import AiraPendingControls, control_device_info
from .coordinator import AiraDataUpdateCoordinator
from .services import (
    SERVICE_ACTIVATE_DHW_BOOST,
    SERVICE_ACTIVATE_NIGHT_MODE,
    SERVICE_CLEAR_AWAY_MODE,
    SERVICE_DEACTIVATE_DHW_BOOST,
    SERVICE_SET_AWAY_MODE,
)

BUTTONS = (
    ("activate_away", SERVICE_SET_AWAY_MODE, "mdi:home-export-outline"),
    ("clear_away", SERVICE_CLEAR_AWAY_MODE, "mdi:home-import-outline"),
    ("start_dhw_boost", SERVICE_ACTIVATE_DHW_BOOST, "mdi:water-boiler"),
    ("cancel_dhw_boost", SERVICE_DEACTIVATE_DHW_BOOST, "mdi:water-boiler-off"),
    ("activate_night_mode", SERVICE_ACTIVATE_NIGHT_MODE, "mdi:sleep"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons without running commands."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AiraControlButton(data["coordinator"], entry, data["pending_controls"], key, service, icon)
        for key, service, icon in BUTTONS
    ])


class AiraControlButton(CoordinatorEntity, ButtonEntity):
    """Run an existing integration service for this heat pump."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AiraDataUpdateCoordinator, entry: ConfigEntry,
        pending: AiraPendingControls, key: str, service: str, icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._pending = pending
        self._service = service
        self.entity_description = ButtonEntityDescription(key=key, translation_key=key, icon=icon)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_UUID]}_{key}"
        self._attr_device_info = control_device_info(entry)

    @property
    def available(self) -> bool:
        """Require a successful coordinator update and a connected device."""
        return super().available and bool(self.coordinator.data and self.coordinator.data.get("connected"))

    async def async_press(self) -> None:
        """Apply pending values only on an explicit button press."""
        if not self.available:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="device_not_connected",
            )
        data = {"entity_id": self.entity_id}
        if self._service == SERVICE_SET_AWAY_MODE:
            data.update(
                start_date=self._pending.away_start_date,
                end_date=self._pending.away_end_date,
            )
        elif self._service == SERVICE_ACTIVATE_DHW_BOOST:
            data["hours"] = int(self._pending.dhw_boost_duration)
        await self.hass.services.async_call(DOMAIN, self._service, data, blocking=True)
