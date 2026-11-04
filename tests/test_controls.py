"""Exercise local selections and explicit controls through Home Assistant."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest import active_boost_data
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pyairahome.commands import (
    ActivateHotWaterBoosting,
    ActivateNightModeForOneHour,
    ClearAwayMode,
    DeactivateHotWaterBoosting,
    SetAwayMode,
)

from custom_components import airahome
from custom_components.airahome import button, select
from custom_components.airahome import date as date_platform
from custom_components.airahome.services import DHW_BOOST_VALID_HOURS


async def test_setup_and_restore_are_local(hass, entry, runtime, add_platform):
    """Integration setup and restoring controls must never send control commands."""
    coordinator = runtime["coordinator"]
    aira = runtime["aira"]
    coordinator.async_config_entry_first_refresh = AsyncMock()
    with (
        patch.object(airahome, "AiraHome", return_value=aira),
        patch.object(airahome, "connect_with_cache_retry", new=AsyncMock(return_value=True)),
        patch.object(airahome.bluetooth, "async_ble_device_from_address", return_value=Mock()),
        patch.object(airahome, "AiraDataUpdateCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()) as forward,
    ):
        assert await airahome.async_setup_entry(hass, entry)
    forward.assert_awaited_once_with(entry, airahome.PLATFORMS)
    assert {"date", "select", "button"} <= set(airahome.PLATFORMS)
    dates = await add_platform(date_platform, {
        "away_start_date": "2030-06-01", "away_end_date": "2030-06-08",
    })
    durations = await add_platform(select, {"dhw_boost_duration": "6"})
    buttons = await add_platform(button)
    assert dates["away_start_date"].native_value == date(2030, 6, 1)
    assert dates["away_end_date"].native_value == date(2030, 6, 8)
    assert durations["dhw_boost_duration"].current_option == "6"
    assert len(buttons) == 5
    aira.ble._run_command.assert_not_awaited()
    assert aira.method_calls == []
    coordinator.async_refresh.assert_not_awaited()
    assert coordinator.data["state"] == {}
    assert not buttons["cancel_dhw_boost"].available
    assert not buttons["activate_night_mode"].available
    assert all(e.available for key, e in buttons.items() if key not in {"cancel_dhw_boost", "activate_night_mode"})
    registry = er.async_get(hass)
    for entity in [*dates.values(), durations["dhw_boost_duration"], *buttons.values()]:
        registered = registry.async_get(entity.entity_id)
        assert registered.unique_id == entity.unique_id
        assert registered.config_entry_id == entry.entry_id
        assert entity.translation_key == entity.unique_id.removeprefix("test-pump_")


@pytest.mark.parametrize("restored", [None, {"away_start_date": "unknown", "away_end_date": "bad", "dhw_boost_duration": "5"}])
async def test_defaults_and_invalid_restore(runtime, add_platform, restored):
    dates = await add_platform(date_platform, restored)
    durations = await add_platform(select, restored)
    assert dates["away_start_date"].native_value == dt_util.now().date()
    assert dates["away_end_date"].native_value == dt_util.now().date() + timedelta(days=3)
    assert durations["dhw_boost_duration"].current_option == "1"
    runtime["aira"].ble._run_command.assert_not_awaited()


@pytest.mark.parametrize("hours", DHW_BOOST_VALID_HOURS)
async def test_boost_duration_and_start(hass, runtime, add_platform, hours):
    duration = (await add_platform(select))["dhw_boost_duration"]
    buttons = await add_platform(button)
    await duration.async_select_option(str(hours))
    assert hass.states.get(duration.entity_id).state == str(hours)
    runtime["aira"].ble._run_command.assert_not_awaited()
    await buttons["start_dhw_boost"].async_press()
    command = runtime["aira"].ble._run_command.call_args.kwargs["command_in"]
    assert isinstance(command, ActivateHotWaterBoosting)
    assert command.to_message().hot_water_boost_duration.seconds == hours * 3600
    runtime["coordinator"].async_refresh.assert_awaited_once()
    assert runtime["coordinator"].data["state"] == {}


async def test_away_dates_apply_only_on_press(runtime, add_platform):
    dates = await add_platform(date_platform)
    buttons = await add_platform(button)
    start = dt_util.now().date() + timedelta(days=7)
    end = start + timedelta(days=3)
    await dates["away_start_date"].async_set_value(start)
    await dates["away_end_date"].async_set_value(end)
    runtime["aira"].ble._run_command.assert_not_awaited()
    await buttons["activate_away"].async_press()
    command = runtime["aira"].ble._run_command.call_args.kwargs["command_in"]
    assert isinstance(command, SetAwayMode)
    message = command.to_message()
    assert message.current_time.ToDatetime().isoformat() == f"{start}T00:00:00"
    assert message.end_time.ToDatetime().isoformat() == f"{end}T00:00:00"
    assert message.target_room_temperature == 0.0
    runtime["coordinator"].async_refresh.assert_awaited_once()


@pytest.mark.parametrize(("start_delta", "duration", "error"), [
    (-1, 4, "away_mode_start_in_past"), (0, 2, "away_mode_dates_too_close"),
    (1, -1, "away_mode_dates_too_close"),
])
async def test_invalid_away_dates(runtime, add_platform, start_delta, duration, error):
    buttons = await add_platform(button)
    pending = runtime["pending_controls"]
    pending.away_start_date = dt_util.now().date() + timedelta(days=start_delta)
    pending.away_end_date = pending.away_start_date + timedelta(days=duration)
    with pytest.raises(ServiceValidationError) as exc:
        await buttons["activate_away"].async_press()
    assert exc.value.translation_key == error
    runtime["aira"].ble._run_command.assert_not_awaited()


@pytest.mark.parametrize(("key", "command_type"), [
    ("clear_away", ClearAwayMode),
    ("cancel_dhw_boost", DeactivateHotWaterBoosting),
    ("activate_night_mode", ActivateNightModeForOneHour),
])
async def test_dedicated_buttons(runtime, add_platform, key, command_type):
    if key == "activate_night_mode":
        runtime["coordinator"].data["state"]["night_mode_enabled"] = False
    if key == "cancel_dhw_boost":
        runtime["coordinator"].data = active_boost_data()
    buttons = await add_platform(button)
    await buttons[key].async_press()
    command = runtime["aira"].ble._run_command.call_args.kwargs["command_in"]
    assert isinstance(command, command_type)
    assert command.to_message().SerializeToString() == b""
    runtime["coordinator"].async_refresh.assert_awaited_once()


@pytest.mark.parametrize(("connected", "success"), [(False, True), (True, False)])
async def test_disconnected_buttons_and_local_edits(runtime, add_platform, connected, success):
    buttons = await add_platform(button)
    dates = await add_platform(date_platform)
    duration = (await add_platform(select))["dhw_boost_duration"]
    coordinator = runtime["coordinator"]
    coordinator.data["connected"] = connected
    coordinator.last_update_success = success
    for entity in buttons.values():
        assert not entity.available
        with pytest.raises(HomeAssistantError):
            await entity.async_press()
    assert dates["away_start_date"].available and duration.available
    await duration.async_select_option("8")
    await dates["away_end_date"].async_set_value(date(2030, 6, 1))
    runtime["aira"].ble._run_command.assert_not_awaited()
