"""Pending boost duration and explicit whole-pump mode controls."""
from __future__ import annotations

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyairahome.commands import (
    DisableCoolingFunction,
    DisableHeatingFunction,
    EnableCoolingFunction,
    EnableHeatingFunction,
)

from .const import CONF_DEVICE_UUID, DOMAIN
from .controls import AiraPendingControls, control_device_info
from .coordinator import AiraDataUpdateCoordinator
from .services import DHW_BOOST_VALID_HOURS


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the pending boost duration and one whole-pump mode control."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AiraBoostDuration(entry, data["pending_controls"]),
        AiraPumpMode(data["coordinator"], entry),
    ])


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


class AiraPumpMode(CoordinatorEntity, SelectEntity):
    """Explicit heating/cooling permissions for the entire pump, not a zone."""

    _attr_has_entity_name = True
    _attr_name = "Whole pump heating/cooling mode"
    _attr_icon = "mdi:heat-pump"

    _modes = {
        "PUMP_MODE_STATE_IDLE": (False, False),
        "PUMP_MODE_STATE_HEATING": (True, False),
        "PUMP_MODE_STATE_COOLING": (False, True),
        "PUMP_MODE_STATE_HEATING_COOLING": (True, True),
    }
    _options = {
        "off": (False, False),
        "heating": (True, False),
        "cooling": (False, True),
        "heating_and_cooling": (True, True),
    }

    def __init__(self, coordinator: AiraDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_UUID]}_pump_mode"
        self._attr_device_info = control_device_info(entry)
        self._selection_lock = asyncio.Lock()

    def _configured_modes(self) -> tuple[bool, bool]:
        state = (self.coordinator.data or {}).get("state", {})
        return self._modes.get(state.get("configured_pump_modes"), (False, False))

    @property
    def options(self) -> list[str]:
        """Offer only modes actually configured on this installation."""
        heat, cool = self._configured_modes()
        if not (heat or cool):
            return []
        return [
            option for option, (needs_heat, needs_cool) in self._options.items()
            if (not needs_heat or heat) and (not needs_cool or cool)
        ]

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.command_available
            and bool(self.options)
        )

    @property
    def current_option(self) -> str | None:
        """Report permissions from telemetry, never the last requested option."""
        state = (self.coordinator.data or {}).get("state", {})
        actual = self._modes.get(state.get("allowed_pump_mode_state"))
        if actual is None or not self.options:
            return None
        configured = self._configured_modes()
        effective = tuple(value and supported for value, supported in zip(actual, configured))
        return next(option for option, modes in self._options.items() if modes == effective)

    async def async_select_option(self, option: str) -> None:
        """Send explicit permissions; selecting again never toggles the mode."""
        async with self._selection_lock:
            configured = self._configured_modes()

            def validate() -> None:
                if not self.available or option not in self.options:
                    raise ServiceValidationError("Whole pump mode is unavailable or unsupported")
                if self._configured_modes() != configured:
                    raise ServiceValidationError("Pump configuration changed; refresh before retrying")

            validate()
            heat, cool = self._options[option]
            commands = []
            if configured[0]:
                commands.append((heat, EnableHeatingFunction() if heat else DisableHeatingFunction()))
            if configured[1]:
                commands.append((cool, EnableCoolingFunction() if cool else DisableCoolingFunction()))
            # Disable unwanted functions first; a partial failure stays visible in readback.
            for _, command in sorted(commands, key=lambda item: item[0]):
                await self.coordinator.async_execute_command(command, validate=validate)
