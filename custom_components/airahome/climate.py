"""Climate platform for Aira Heat Pump."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyairahome import AiraHome
from pyairahome.commands import (
    SetZoneSetpoints,
)
from pyairahome.device.heat_pump.command.v1.set_zone_setpoints_pb2 import SetZoneSetpoints as _SetZoneSetpointsPb2, ZoneTemperatures  # type: ignore

from .const import (
    CONF_DEVICE_NAME,
    CONF_DEVICE_UUID,
    CONF_MAC_ADDRESS,
    CONF_NUM_ZONES,
    DEFAULT_NUM_ZONES,
    DEFAULT_SHORT_NAME,
    DOMAIN,
)
from .coordinator import AiraDataUpdateCoordinator
from .freshness import AiraChannelEntity

Kind = _SetZoneSetpointsPb2.Kind


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aira climate platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    aira = hass.data[DOMAIN][entry.entry_id]["aira"]

    num_zones = entry.options.get(CONF_NUM_ZONES, DEFAULT_NUM_ZONES)
    _LOGGER.debug("Setting up climate entities for %d zones based on config entry options", num_zones)

    entities: list[ClimateEntity] = []
    for i in range(1, num_zones + 1):
        entities.append(
            AiraZoneClimate(
                coordinator, entry, aira,
                zone=i,
            )
        )

    async_add_entities(entities, True)


# ============================================================================
# BASE CLIMATE CLASS
# ============================================================================

class AiraClimateBase(AiraChannelEntity, CoordinatorEntity, ClimateEntity):  # type: ignore
    """Base class for Aira climate entities."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: AiraDataUpdateCoordinator,
        entry: ConfigEntry,
        aira: AiraHome,
        unique_id_suffix: str,
    ) -> None:
        """Initialise the climate entity."""
        super().__init__(coordinator)
        self._device_uuid = entry.data[CONF_DEVICE_UUID]
        self._attr_unique_id = f"{self._device_uuid}_{unique_id_suffix}"
        self._attr_translation_key = unique_id_suffix
        self.aira = aira

        self._attr_device_info = DeviceInfo(**{
            "identifiers": {(DOMAIN, self._device_uuid)},
            "connections": {(dr.CONNECTION_BLUETOOTH, entry.data.get(CONF_MAC_ADDRESS))},
            "name": entry.data.get(CONF_DEVICE_NAME, DEFAULT_SHORT_NAME),
            "manufacturer": "Aira",
            "model": "Heat Pump",
        })

# ============================================================================
# ZONE CLIMATE ENTITY
# ============================================================================

class AiraZoneClimate(AiraClimateBase):
    """Climate entity representing a single Aira heating/cooling zone."""

    _attr_target_temperature_step = 0.5
    _attr_min_temp = 10.0
    _attr_max_temp = 30.0
    _attr_precision = 0.1

    def __init__(
        self,
        coordinator: AiraDataUpdateCoordinator,
        entry: ConfigEntry,
        aira: AiraHome,
        zone: int,
    ) -> None:
        """Initialise the zone climate entity."""
        unique_id_suffix = f"zone_{zone}_climate"
        super().__init__(coordinator, entry, aira, unique_id_suffix)

        self._zone = zone

        # Pump permissions are reported here, but controlled only by the pump select.
        self._attr_hvac_modes = []
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    # Internal helpers
    def _get_thermostat_field(self, field: str) -> Any:
        """Read a field from this zone's thermostat last_update list entry."""
        try:
            updates = self.coordinator.data["state"]["thermostats"]
            if isinstance(updates, list):
                for element in updates:
                    if element.get("zone") == f"ZONE_{self._zone}":
                        if element.get("rssi") == 0:
                            return None
                        return element.get(field)
        except (KeyError, TypeError):
            pass
        return None
    
    async def _set_setpoints(self, heating: float | None, cooling: float | None) -> None:
        """Send a command to set the heating/cooling setpoints for this zone."""
        # Plain ValueError, not HomeAssistantError: this is a bug in our own calling code, not a device/BLE failure. Should never happen.
        if heating is not None and cooling is not None:
            raise ValueError(f"Can't set both heating and cooling setpoints at the same time due to device limitations. Received heating: {heating}, cooling: {cooling}")
        if heating is None and cooling is None:
            raise ValueError(f"No setpoint provided to set_setpoints. Received heating: {heating}, cooling: {cooling}")

        temperature = heating if heating is not None else cooling

        zone = f"zone_{self._zone}"
        command_in = SetZoneSetpoints(
            zone_setpoints=ZoneTemperatures(
                **{zone: temperature}
            ),
            # NB: Aira uses the heating setpoint for both cooling and heating
            # Kind.KIND_HEATING if heating is not None else Kind.KIND_COOLING
            kind=Kind.KIND_HEATING
        )

        await self.coordinator.async_execute_command(command_in)

    # State properties
    @property
    def current_temperature(self) -> float | None:  # type: ignore
        """Return the current temperature from the zone thermostat."""
        if not self.coordinator.data:
            return None
        try:
            raw = self._get_thermostat_field("last_update").get("actual_temperature")
            if raw is not None:
                return round(float(raw) / 10, 2)
        except (ValueError, TypeError, AttributeError):
            pass
        return None

    @property
    def current_humidity(self) -> float | None:  # type: ignore
        """Return the current humidity from the zone thermostat."""
        if not self.coordinator.data:
            return None
        try:
            raw = self._get_thermostat_field("last_update").get("humidity")
            if raw is not None:
                return round(float(raw) / 10, 1)
        except (ValueError, TypeError, AttributeError):
            pass
        return None
    
    @property
    def target_temperature(self) -> float | None:  # type: ignore
        """Return the target temperature (always the heating setpoint)."""
        if not self.coordinator.data:
            return None
        try:
            value = self.coordinator.data.get("state", {}).get("zone_setpoints_heating", {}).get(f"zone_{self._zone}")
            return round(float(value), 2) if value is not None else None
        except (KeyError, ValueError, TypeError):
            return None

    @property
    def hvac_mode(self) -> HVACMode | None:  # type: ignore
        """Report permissions using the current, fresh installation configuration."""
        if not self.coordinator.channel_available("state"):
            return None
        state = self.coordinator.data.get("state", {})
        configured = state.get("configured_pump_modes")
        allowed = state.get("allowed_pump_mode_state")
        known_modes = (
            "PUMP_MODE_STATE_IDLE",
            "PUMP_MODE_STATE_HEATING",
            "PUMP_MODE_STATE_COOLING",
            "PUMP_MODE_STATE_HEATING_COOLING",
        )
        if configured not in known_modes or allowed not in known_modes:
            return None
        has_heat = "HEATING" in configured and "HEATING" in allowed
        has_cool = "COOLING" in configured and "COOLING" in allowed
        if has_heat and has_cool:
            return HVACMode.HEAT_COOL
        if has_heat:
            return HVACMode.HEAT
        if has_cool:
            return HVACMode.COOL
        return HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:  # type: ignore
        """Return what the heat pump is currently doing (global active state)."""
        if not self.coordinator.data:
            return None
        try:
            active = self.coordinator.data.get("state", {}).get("pump_active_state", "")
            if active == "PUMP_ACTIVE_STATE_HEATING":
                return HVACAction.HEATING
            if active == "PUMP_ACTIVE_STATE_COOLING":  # device should never report this for heating-only units, but if it does the action will be wrong
                return HVACAction.COOLING
            if active == "PUMP_ACTIVE_STATE_DEFROSTING":
                return HVACAction.DEFROSTING
            if self.hvac_mode == HVACMode.OFF:
                return HVACAction.OFF
        except (KeyError, TypeError):
            pass
        return HVACAction.IDLE
    
    # Service calls
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the zone target temperature (always the heating setpoint)."""
        if not self.coordinator.data:
            return

        setpoint = kwargs.get(ATTR_TEMPERATURE)
        if setpoint is None:
            return

        if not (self._attr_min_temp <= setpoint <= self._attr_max_temp):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="temperature_out_of_range",
                translation_placeholders={
                    "temperature": str(setpoint),
                    "min_temp": str(self._attr_min_temp),
                    "max_temp": str(self._attr_max_temp),
                }
            )

        _LOGGER.debug("Received set_temperature call with kwargs: %s", kwargs)

        await self._set_setpoints(setpoint, None)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Reject pump-wide changes through a zone entity."""
        raise ServiceValidationError("Use the Whole pump heating/cooling mode select")

    async def async_turn_on(self) -> None:
        """A zone has no independent on/off command."""
        raise ServiceValidationError("Use the Whole pump heating/cooling mode select")

    async def async_turn_off(self) -> None:
        """A zone has no independent on/off command."""
        raise ServiceValidationError("Use the Whole pump heating/cooling mode select")
