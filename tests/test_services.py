"""Service target resolution and real SDK command acknowledgements."""
from unittest.mock import AsyncMock

import pytest
from conftest import active_boost_data
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyairahome.device.heat_pump.command.v1.command_progress_pb2 import CommandProgress
from pyairahome.utils import Utils
from pyairahome.utils.exceptions import BLEConnectionError

from custom_components.airahome import button
from custom_components.airahome.const import DOMAIN


@pytest.mark.parametrize("updates", [
    [], [{"received": {}}], [{"error": {"message": "rejected"}}],
    [{"succeeded": False}], [{"succeeded": None}],
    [{"succeeded": {}, "error": {"message": "rejected"}}],
])
async def test_failed_ack_does_not_refresh(hass, runtime, add_platform, updates):
    buttons = await add_platform(button)

    async def run(**kwargs):
        async def progress():
            for update in updates:
                yield update
        return progress()

    runtime["aira"].ble._run_command.side_effect = run
    with pytest.raises(HomeAssistantError) as exc:
        await buttons["clear_away"].async_press()
    assert exc.value.translation_key == "away_mode_clear_failed"
    runtime["coordinator"].async_request_refresh.assert_not_awaited()
    assert runtime["coordinator"].data["state"] == {}


@pytest.mark.parametrize("during_stream", [False, True])
@pytest.mark.parametrize("error_type", [TimeoutError, BLEConnectionError])
async def test_transport_failures(runtime, add_platform, during_stream, error_type):
    runtime["coordinator"].data = active_boost_data()
    buttons = await add_platform(button)

    async def fail(**kwargs):
        if not during_stream:
            raise error_type("disconnected")
        async def progress():
            yield {"received": {}}
            raise error_type("disconnected")
        return progress()

    runtime["aira"].ble._run_command.side_effect = fail
    with pytest.raises(HomeAssistantError) as exc:
        await buttons["cancel_dhw_boost"].async_press()
    assert exc.value.translation_key == "dhw_boost_deactivate_failed"
    runtime["coordinator"].async_request_refresh.assert_not_awaited()


async def test_sdk_empty_success_and_stream_closes_before_refresh(runtime, add_platform):
    """Use the SDK protobuf conversion, including its empty success marker."""
    buttons = await add_platform(button)
    runtime["coordinator"].data["state"]["night_mode_enabled"] = False
    closed = False
    message = CommandProgress()
    message.succeeded.SetInParent()
    ack = Utils.convert_to_dict(message)
    assert ack["succeeded"] == {}

    async def run(**kwargs):
        async def progress():
            nonlocal closed
            try:
                yield {"received": {}}
                yield ack
            finally:
                closed = True
        return progress()

    async def refresh():
        assert closed

    runtime["aira"].ble._run_command.side_effect = run
    runtime["coordinator"].async_request_refresh = AsyncMock(side_effect=refresh)
    await buttons["activate_night_mode"].async_press()
    runtime["coordinator"].async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize("target", [{}, {"entity_id": []}, {"entity_id": "button.missing"}, {"device_id": "missing"}, {"area_id": "missing"}])
async def test_empty_targets_raise(hass, runtime, target):
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(DOMAIN, "deactivate_dhw_boost", target, blocking=True)
    assert exc.value.translation_key == "no_aira_target"
    runtime["aira"].ble._run_command.assert_not_awaited()


@pytest.mark.parametrize("target_type", ["entity", "device", "area", "combined"])
async def test_targets_and_deduplication(hass, runtime, add_platform, target_type):
    runtime["coordinator"].data = active_boost_data()
    buttons = await add_platform(button)
    entity_ids = [entity.entity_id for entity in buttons.values()]
    device_id = er.async_get(hass).async_get(entity_ids[0]).device_id
    area = ar.async_get(hass).async_create("Utility")
    dr.async_get(hass).async_update_device(device_id, area_id=area.id)
    targets = {
        "entity": {"entity_id": entity_ids},
        "device": {"device_id": device_id},
        "area": {"area_id": area.id},
        "combined": {"entity_id": entity_ids, "device_id": device_id, "area_id": area.id},
    }
    await hass.services.async_call(DOMAIN, "deactivate_dhw_boost", targets[target_type], blocking=True)
    runtime["aira"].ble._run_command.assert_awaited_once()
    runtime["coordinator"].async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize("hours", [0, 5, 25, "invalid"])
async def test_invalid_service_duration(hass, runtime, add_platform, hours):
    buttons = await add_platform(button)
    import voluptuous as vol
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "activate_dhw_boost", {
            "entity_id": buttons["start_dhw_boost"].entity_id, "hours": hours,
        }, blocking=True)
    runtime["aira"].ble._run_command.assert_not_awaited()
