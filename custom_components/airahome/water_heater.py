"""Water heater platform for Aira Heat Pump."""
from __future__ import annotations

import asyncio
import math
from typing import Any

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.components.water_heater.const import STATE_ELECTRIC, STATE_HEAT_PUMP, STATE_PERFORMANCE
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyairahome import AiraHome
from pyairahome.commands import SetTargetHotWaterTemperature

from .const import CONF_DEVICE_NAME, CONF_DEVICE_UUID, CONF_INSTALLATION, CONF_MAC_ADDRESS, DEFAULT_SHORT_NAME, DOMAIN
from .coordinator import AiraDataUpdateCoordinator
from .freshness import AiraChannelEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aira water heater platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    aira = hass.data[DOMAIN][entry.entry_id]["aira"]
    
    water_heaters: list[WaterHeaterEntity] = [
        AiraWaterHeater(coordinator, entry, aira)
    ]
    
    async_add_entities(water_heaters, True)


# ============================================================================
# WATER HEATER
# ============================================================================

class AiraWaterHeater(AiraChannelEntity, CoordinatorEntity, WaterHeaterEntity): # type: ignore
    """Representation of an Aira Heat Pump DHW (Domestic Hot Water) system."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
    )
    _attr_operation_list = [
        STATE_OFF,
        STATE_PERFORMANCE,
        STATE_HEAT_PUMP,
        STATE_ELECTRIC,
    ]
    _attr_max_temp = 65
    _attr_min_temp = 15 # min to 15 because schedule can take it to 15, but we don't allow the user to set it
    _attr_target_temperature_step = 0.1
    
    _allowed_temperatures = [50, 55, 65]  # default; overridden based on tank size in __init__

    def __init__(
        self,
        coordinator: AiraDataUpdateCoordinator,
        entry: ConfigEntry,
        aira: AiraHome
    ) -> None:
        """Initialize the water heater."""
        super().__init__(coordinator)
        self._device_uuid = entry.data[CONF_DEVICE_UUID]
        tank_size = entry.data.get(CONF_INSTALLATION, {}).get("tank_size", "")
        if "100" in tank_size: # since 100 liters is very small aira allows different setpoints
            self._allowed_temperatures = [55, 60, 65]
        unique_id_suffix = "water_heater"
        self._attr_unique_id = f"{self._device_uuid}_{unique_id_suffix}"
        self._attr_translation_key = unique_id_suffix
        self.aira = aira
        self._temperature_lock = asyncio.Lock()

        self._attr_device_info = DeviceInfo(**{
            "identifiers": {(DOMAIN, self._device_uuid)},
            "connections": {(dr.CONNECTION_BLUETOOTH, entry.data.get(CONF_MAC_ADDRESS))},
            "name": entry.data.get(CONF_DEVICE_NAME, DEFAULT_SHORT_NAME),
            "manufacturer": "Aira",
            "model": "Heat Pump",
        })

    @property
    def current_temperature(self): # type: ignore
        """Return the current temperature."""
        if not self.coordinator.data:
            return None
        
        try:
            return round(float(self.coordinator.data["state"]["current_hot_water_temperature"]), 2)
        except (KeyError, ValueError, TypeError):
            return None
    
    @property
    def target_temperature(self) -> float | None:
        """Read the scheduled override or manual target from reported state."""
        try:
            state = self.coordinator.data.get("state", {})
            value = state.get("target_hot_water_temperature")
            for action in state.get("scheduler", {}).get("active_actions", []):
                if "set_dhw_setpoint" in action:
                    value = action["set_dhw_setpoint"].get("temperature")
                    break
            result = float(value) if value is not None else None
            return round(result, 2) if result is not None and math.isfinite(result) else None
        except (AttributeError, ValueError, TypeError):
            return None

    def _validate_manual_target(self, previous: float | None) -> None:
        """Recheck scheduler ownership and the target under the command lock."""
        state = self.coordinator.data.get("state", {})
        for action in state.get("scheduler", {}).get("active_actions", []):
            if "set_dhw_setpoint" in action:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="scheduler_active",
                )
        if previous is None or self.target_temperature != previous:
            raise ServiceValidationError("Hot water target changed or is unknown; refresh before retrying")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set an explicit target and await reported readback, without optimism."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="temperature_not_provided",
            )
        try:
            requested = float(temperature)
        except (ValueError, TypeError) as err:
            raise ServiceValidationError("Invalid hot water temperature") from err
        if not math.isfinite(requested):
            raise ServiceValidationError("Invalid hot water temperature")

        previous = self.target_temperature
        async with self._temperature_lock:
            self._validate_manual_target(previous)
            if math.isclose(abs(requested - previous), self._attr_target_temperature_step):
                candidates = [
                    value for value in self._allowed_temperatures
                    if (value > previous if requested > previous else value < previous)
                ]
                if not candidates:
                    return
                target = min(candidates) if requested > previous else max(candidates)
            else:
                target = requested
                if target not in self._allowed_temperatures:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="invalid_temperature",
                        translation_placeholders={
                            "allowed_temperatures": ", ".join(map(str, self._allowed_temperatures)),
                            "temperature": str(temperature),
                        },
                    )
            await self.coordinator.async_execute_command(
                SetTargetHotWaterTemperature(temperature=target),
                validate=lambda: self._validate_manual_target(previous),
            )
            # An ACK confirms receipt, not that the requested target was read back.
            if self.target_temperature != target:
                raise HomeAssistantError("Hot water command acknowledged, but target readback is unconfirmed")

    @property
    def current_operation(self): # type: ignore
        """Return current operation status."""
        status = None
        if not self.coordinator.data:
            return status
        
        if not self.coordinator.data.get("state", {}).get("hot_water", {}).get("heating_enabled"):
            status = STATE_OFF
        else:
            heatpump = False
            inline_heater = False

            pump_active_state = self.coordinator.data.get("state", {}).get("pump_active_state", False)
            if pump_active_state == "PUMP_ACTIVE_STATE_DHW":
                heatpump = True
            inline_heater_state = self.coordinator.data.get("state", {}).get("inline_heater_active", False)
            
            if inline_heater_state:
                inline_heater = True
            if heatpump and inline_heater:
                status = STATE_PERFORMANCE
            elif heatpump:
                status = STATE_HEAT_PUMP
            elif inline_heater:
                status = STATE_ELECTRIC
            else:
                status = STATE_OFF

        return status
