"""Button availability and services must agree before any device command."""
from datetime import timedelta

import pytest
from conftest import active_boost_data
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pyairahome.device.heat_pump.state.v1.state_pb2 import State
from pyairahome.utils import Utils

from custom_components.airahome import button
from custom_components.airahome.binary_sensor import AiraWaterBoostActiveSensor
from custom_components.airahome.const import DOMAIN


@pytest.mark.parametrize(("schedules", "expected"), [
    ([], False), ([{"boost": {"events": []}}], False),
    ([{"boost": {"events": [{"active": False}]}}], False),
    ([{"away_mode": {"events": []}}], False),
    (None, None), ({}, None), ([None], None), ([{}], None),
    ([{"boost": None}], None), ([{"boost": {}}], None),
    ([{"boost": {"events": {}}}], None),
    ([{"boost": {"events": [{}]}}], None),
    ([{"boost": {"events": [{"active": "false"}]}}], None),
    ([{"boost": {"events": [{"active": True}]}}], None),
])
async def test_boost_state_matrix(hass, entry, runtime, add_platform, schedules, expected):
    coordinator = runtime["coordinator"]
    coordinator.data = {"connected": True, "state": {"scheduler": {"schedules": schedules}}}
    buttons = await add_platform(button)
    assert AiraWaterBoostActiveSensor(coordinator, entry).is_on is expected
    assert buttons["start_dhw_boost"].available
    assert not buttons["cancel_dhw_boost"].available
    for press in [
        buttons["cancel_dhw_boost"].async_press,
        lambda: hass.services.async_call(DOMAIN, "deactivate_dhw_boost", {
            "entity_id": buttons["cancel_dhw_boost"].entity_id,
        }, blocking=True),
    ]:
        with pytest.raises(ServiceValidationError):
            await press()
    runtime["aira"].ble._run_command.assert_not_awaited()
    await buttons["start_dhw_boost"].async_press()
    runtime["aira"].ble._run_command.assert_awaited_once()


@pytest.mark.parametrize("seconds", [-1, 0, 60])
async def test_expiry_rechecked_before_press(hass, entry, runtime, add_platform, seconds):
    coordinator = runtime["coordinator"]
    coordinator.data = active_boost_data()
    buttons = await add_platform(button)
    assert buttons["cancel_dhw_boost"].available
    assert not buttons["start_dhw_boost"].available
    end = dt_util.utcnow() + timedelta(seconds=seconds)
    coordinator.data["state"]["scheduler"]["schedules"][0]["boost"]["events"][0]["event_end_dt"] = end
    active = seconds > 0
    assert AiraWaterBoostActiveSensor(coordinator, entry).is_on is active
    assert buttons["cancel_dhw_boost"].available is active
    assert buttons["start_dhw_boost"].available is not active
    key, service = ("start_dhw_boost", "activate_dhw_boost") if active else ("cancel_dhw_boost", "deactivate_dhw_boost")
    with pytest.raises(ServiceValidationError):
        await buttons[key].async_press()
    data = {"entity_id": buttons[key].entity_id}
    if active:
        data["hours"] = 1
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    runtime["aira"].ble._run_command.assert_not_awaited()


@pytest.mark.parametrize("night", [False, True, None, 0, 1, "false"])
async def test_night_requires_reported_false(hass, runtime, add_platform, night):
    coordinator = runtime["coordinator"]
    coordinator.data["state"]["night_mode_enabled"] = False
    control = (await add_platform(button))["activate_night_mode"]
    assert control.available
    coordinator.data["state"]["night_mode_enabled"] = night
    assert control.available is (night is False)
    if night is False:
        await control.async_press()
        runtime["aira"].ble._run_command.assert_awaited_once()
        return
    with pytest.raises(ServiceValidationError):
        await control.async_press()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "activate_night_mode", {"entity_id": control.entity_id}, blocking=True)
    runtime["aira"].ble._run_command.assert_not_awaited()


async def test_proto_defaults_and_future_away(runtime, add_platform):
    state = State()
    state.scheduler.SetInParent()
    converted = Utils.convert_to_dict(state)
    assert converted["night_mode_enabled"] is False
    assert converted["away_mode_enabled"] is False
    assert converted["scheduler"]["schedules"] == []
    runtime["coordinator"].data = {"connected": True, "state": converted}
    buttons = await add_platform(button)
    assert buttons["start_dhw_boost"].available
    assert buttons["activate_night_mode"].available
    assert not buttons["cancel_dhw_boost"].available
    converted["scheduler"]["schedules"] = [{"away_mode": {"events": [{
        "active": False, "event_start_dt": dt_util.now() + timedelta(days=7),
    }]}}]
    assert buttons["clear_away"].available and buttons["activate_away"].available
    await buttons["clear_away"].async_press()
    runtime["aira"].ble._run_command.assert_awaited_once()


@pytest.mark.parametrize(("connected", "success"), [(False, True), (True, False)])
async def test_offline_service_guards(hass, runtime, add_platform, connected, success):
    coordinator = runtime["coordinator"]
    coordinator.data = active_boost_data()
    coordinator.data["state"]["night_mode_enabled"] = False
    buttons = await add_platform(button)
    coordinator.data["connected"] = connected
    coordinator.last_update_success = success
    for key, service in [
        ("cancel_dhw_boost", "deactivate_dhw_boost"),
        ("start_dhw_boost", "activate_dhw_boost"),
        ("activate_night_mode", "activate_night_mode"),
    ]:
        assert not buttons[key].available
        data = {"entity_id": buttons[key].entity_id}
        if key == "start_dhw_boost":
            data["hours"] = 1
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
    runtime["aira"].ble._run_command.assert_not_awaited()
