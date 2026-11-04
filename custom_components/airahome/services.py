"""Services for the Aira Home integration."""
from __future__ import annotations

import datetime
from functools import partial

import voluptuous as vol
from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids
from homeassistant.util import dt as dt_util
from pyairahome.commands import (
    ActivateHotWaterBoosting,
    ActivateNightModeForOneHour,
    ClearAwayMode,
    CommandBase,
    DeactivateHotWaterBoosting,
    SetAwayMode,
)
from pyairahome.utils.exceptions import BLEConnectionError

from .boost import boost_active
from .const import DOMAIN
from .coordinator import AiraDataUpdateCoordinator

SERVICE_ACTIVATE_DHW_BOOST = "activate_dhw_boost"
SERVICE_DEACTIVATE_DHW_BOOST = "deactivate_dhw_boost"
SERVICE_SET_AWAY_MODE = "set_away_mode"
SERVICE_CLEAR_AWAY_MODE = "clear_away_mode"
SERVICE_ACTIVATE_NIGHT_MODE = "activate_night_mode"

DHW_BOOST_VALID_HOURS = [1, 2, 3, 4, 6, 8, 12, 24]
AWAY_MODE_MIN_DAYS = 3

_TARGET_SCHEMA = {
    vol.Optional("entity_id"): vol.Any(cv.string, [cv.string]),
    vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
    vol.Optional("area_id"): vol.Any(cv.string, [cv.string]),
}

ACTIVATE_DHW_BOOST_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Required("hours"): vol.All(vol.Coerce(int), vol.In(DHW_BOOST_VALID_HOURS)),
    }
)
DEACTIVATE_DHW_BOOST_SCHEMA = vol.Schema(_TARGET_SCHEMA)
SET_AWAY_MODE_SCHEMA = vol.Schema({
    **_TARGET_SCHEMA,
    vol.Required("start_date"): cv.date,
    vol.Required("end_date"): cv.date,
})

SERVICES = [
    SERVICE_ACTIVATE_DHW_BOOST,
    SERVICE_DEACTIVATE_DHW_BOOST,
    SERVICE_SET_AWAY_MODE,
    SERVICE_CLEAR_AWAY_MODE,
    SERVICE_ACTIVATE_NIGHT_MODE,
]


def action_unavailable_reason(coordinator: AiraDataUpdateCoordinator, service: str) -> str | None:
    """Share button availability and service validation against reported state."""
    data = coordinator.data
    if not coordinator.last_update_success or not coordinator.command_available:
        return "device_not_connected"
    if service == SERVICE_DEACTIVATE_DHW_BOOST and boost_active(data) is not True:
        return "no_active_dhw_boost"
    if service == SERVICE_ACTIVATE_DHW_BOOST and boost_active(data) is True:
        return "dhw_boost_already_active"
    if service == SERVICE_ACTIVATE_NIGHT_MODE:
        state = data.get("state")
        night = state.get("night_mode_enabled") if isinstance(state, dict) else None
        if night is True:
            return "night_mode_already_active"
        if night is not False:
            return "night_mode_state_unknown"
    return None


async def _get_coordinators_from_target(
    hass: HomeAssistant, call: ServiceCall,
) -> list[AiraDataUpdateCoordinator]:
    """Resolve explicit entity, device and area targets, removing duplicates."""
    domain_data = hass.data.get(DOMAIN, {})
    entry_ids = (
        await async_extract_config_entry_ids(call)
        if any(call.data.get(key) for key in ("entity_id", "device_id", "area_id"))
        else set()
    )
    coordinators = [
        domain_data[entry_id]["coordinator"]
        for entry_id in sorted(entry_ids)
        if entry_id in domain_data
    ]
    if not coordinators:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_aira_target",
        )
    return coordinators


async def _async_run_command(
    coordinator: AiraDataUpdateCoordinator,
    command_in: CommandBase,
    service: str,
    translation_key: str,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Consume command progress and refresh only after a successful ACK."""
    if reason := action_unavailable_reason(coordinator, service):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=reason,
        )

    def validate() -> None:
        if reason := action_unavailable_reason(coordinator, service):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key=reason)

    try:
        await coordinator.async_execute_command(command_in, validate=validate)
    except ServiceValidationError:
        raise
    except (BLEConnectionError, TimeoutError, HomeAssistantError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders={**(placeholders or {}), "error": str(err)},
        ) from err


async def _handle_activate_dhw_boost(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the activate_dhw_boost service call."""
    hours: int = call.data["hours"]
    for coordinator in await _get_coordinators_from_target(hass, call):
        await _async_run_command(
            coordinator,
            ActivateHotWaterBoosting(hot_water_boost_duration=Duration(seconds=hours * 3600)),
            SERVICE_ACTIVATE_DHW_BOOST, "dhw_boost_activate_failed",
            {"hours": str(hours)},
        )


def _date_to_timestamp(d: datetime.date) -> Timestamp:
    # Aira uses timestamps with time set to 00.00.00 for dates.
    dt = datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.UTC)
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


async def _handle_set_away_mode(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the set_away_mode service call."""
    start_date: datetime.date = call.data["start_date"]
    end_date: datetime.date = call.data["end_date"]
    if start_date < dt_util.now().date():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="away_mode_start_in_past",
        )
    if (end_date - start_date).days < AWAY_MODE_MIN_DAYS:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="away_mode_dates_too_close",
            translation_placeholders={"min_days": str(AWAY_MODE_MIN_DAYS)},
        )

    for coordinator in await _get_coordinators_from_target(hass, call):
        await _async_run_command(
            coordinator,
            SetAwayMode(
                current_time=_date_to_timestamp(start_date),
                end_time=_date_to_timestamp(end_date),
                target_room_temperature=0.0,  # Matches the app's away mode behaviour.
            ),
            SERVICE_SET_AWAY_MODE, "away_mode_set_failed",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )


async def _handle_deactivate_dhw_boost(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the deactivate_dhw_boost service call."""
    for coordinator in await _get_coordinators_from_target(hass, call):
        await _async_run_command(
            coordinator, DeactivateHotWaterBoosting(), SERVICE_DEACTIVATE_DHW_BOOST, "dhw_boost_deactivate_failed",
        )


async def _handle_clear_away_mode(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the clear_away_mode service call."""
    for coordinator in await _get_coordinators_from_target(hass, call):
        await _async_run_command(coordinator, ClearAwayMode(), SERVICE_CLEAR_AWAY_MODE, "away_mode_clear_failed")


async def _handle_activate_night_mode(hass: HomeAssistant, call: ServiceCall) -> None:
    """Activate night mode for the SDK's fixed duration of one hour."""
    for coordinator in await _get_coordinators_from_target(hass, call):
        await _async_run_command(
            coordinator, ActivateNightModeForOneHour(), SERVICE_ACTIVATE_NIGHT_MODE, "night_mode_activate_failed",
        )


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register airahome services."""
    for service, handler, schema in (
        (SERVICE_ACTIVATE_DHW_BOOST, _handle_activate_dhw_boost, ACTIVATE_DHW_BOOST_SCHEMA),
        (SERVICE_DEACTIVATE_DHW_BOOST, _handle_deactivate_dhw_boost, DEACTIVATE_DHW_BOOST_SCHEMA),
        (SERVICE_SET_AWAY_MODE, _handle_set_away_mode, SET_AWAY_MODE_SCHEMA),
        (SERVICE_CLEAR_AWAY_MODE, _handle_clear_away_mode, vol.Schema(_TARGET_SCHEMA)),
        (SERVICE_ACTIVATE_NIGHT_MODE, _handle_activate_night_mode, vol.Schema(_TARGET_SCHEMA)),
    ):
        hass.services.async_register(DOMAIN, service, partial(handler, hass), schema=schema)
