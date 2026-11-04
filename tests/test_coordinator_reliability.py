"""Independent response freshness and BLE lifecycle ownership."""

import asyncio
from time import perf_counter
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.airahome.binary_sensor import AiraProblemBinarySensor
from custom_components.airahome.coordinator import AiraDataUpdateCoordinator
from custom_components.airahome.sensor import AiraTemperatureSensor


@pytest.fixture
def coordinator(hass, entry):
    aira = Mock()
    aira.ble._get_states = AsyncMock(return_value={"state": {"room": 20}})
    aira.ble._get_system_check_state = AsyncMock(
        return_value={"system_check_state": {"errors": [], "diagnostic": 1}}
    )
    aira.ble._disconnect = AsyncMock()
    aira.ble._clear_cache = AsyncMock(return_value=True)
    aira.ble._connect_device = AsyncMock(return_value=True)
    coord = AiraDataUpdateCoordinator(
        hass, entry, aira, mac_address="00:11:22:33:44:55"
    )
    coord.update_interval = None
    return coord


@pytest.mark.parametrize("prior_age", [None, 100, 601])
async def test_failed_system_read_is_never_complete(coordinator, prior_age):
    c = coordinator
    alarm = {"aira": "AIRA_ERROR_CODE_TEST", "severity": "SEVERITY_ERROR"}
    if prior_age is not None:
        c._channel_data["system_check_state"] = {"errors": [alarm]}
        c._channel_error_codes["system_check_state"] = [alarm]
        c._channel_good["system_check_state"] = perf_counter() - prior_age
    c.aira.ble._get_system_check_state.side_effect = TimeoutError()
    with patch("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0):
        result = await c._async_update_data()
    c.async_set_updated_data(result)
    assert c.channel_available("state")
    assert not c.channel_available("system_check_state")
    if prior_age is None:
        assert "system_check_state" not in c._channel_good
    else:
        assert perf_counter() - c._channel_good["system_check_state"] >= prior_age
        assert result["system_check_state"]["errors"] == [alarm]
        assert "AIRA_ERROR_CODE_TEST" in c._active_errors


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"state": {}},
        {"state": {"room": 20}, "error": "DENIED"},
        {"state": {"errors": "bad"}},
    ],
)
async def test_bad_state_does_not_gain_freshness(coordinator, bad):
    c = coordinator
    c.aira.ble._get_states.return_value = bad
    with patch("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0):
        result = await c._async_update_data()
    c.async_set_updated_data(result)
    assert not c.channel_available("state")
    assert c.channel_available("system_check_state")
    assert "state" not in c._channel_good
    assert not c.command_available


async def test_only_fresh_system_clear_can_clear_its_alarm(coordinator, entry):
    c = coordinator
    alarm = {"aira": "AIRA_ERROR_CODE_TEST", "severity": "SEVERITY_ERROR"}
    c.aira.ble._get_system_check_state.return_value = {
        "system_check_state": {"errors": [alarm]}
    }
    with patch("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0):
        c.async_set_updated_data(await c._async_update_data())
        entity = AiraProblemBinarySensor(c, entry, "alarms", ("error",))
        assert entity.available and entity.is_on
        c.aira.ble._get_system_check_state.side_effect = TimeoutError()
        c.async_set_updated_data(await c._async_update_data())
        assert not entity.available
        assert entity.is_on
        assert "AIRA_ERROR_CODE_TEST" in c._active_errors
        c._next_system_check = 0
        c.aira.ble._get_system_check_state.side_effect = None
        c.aira.ble._get_system_check_state.return_value = {
            "system_check_state": {"diagnostic": 1, "errors": []}
        }
        c.async_set_updated_data(await c._async_update_data())
        assert entity.available and not entity.is_on
        assert not c._active_errors


async def test_diagnostics_backoff_preserves_normal_poll(coordinator):
    c = coordinator
    c.aira.ble._get_system_check_state.side_effect = TimeoutError()
    with patch("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0):
        for _ in range(3):
            c.async_set_updated_data(await c._async_update_data())
    assert c.aira.ble._get_states.await_count == 3
    assert c.aira.ble._get_system_check_state.await_count == 1
    assert c.channel_available("state")


async def test_sensor_tracks_own_channel(coordinator, entry):
    c = coordinator
    with patch("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0):
        c.async_set_updated_data(await c._async_update_data())
    state = AiraTemperatureSensor(c, entry, "state_temp", ("state", "room"))
    diagnostic = AiraTemperatureSensor(
        c, entry, "system_temp", ("system_check_state", "diagnostic")
    )
    assert state.available and diagnostic.available
    c._channel_failed.add("system_check_state")
    assert state.available and not diagnostic.available
    c.data["connected"] = False
    assert not state.available and not diagnostic.available


async def test_cache_clear_failure_keeps_intent(coordinator):
    c = coordinator
    c._needs_cache_clear = True
    c.aira.ble._clear_cache.side_effect = RuntimeError("failed")
    assert not await c._async_clear_gatt_cache()
    assert c._needs_cache_clear
    c.aira.ble._clear_cache.side_effect = None
    c.aira.ble._clear_cache.return_value = False
    assert not await c._async_clear_gatt_cache()
    assert c._needs_cache_clear
    c.aira.ble._clear_cache.return_value = True
    assert await c._async_clear_gatt_cache()
    assert not c._needs_cache_clear


async def test_gatt_error_partial_success_schedules_paced_recovery(coordinator):
    c = coordinator
    c.aira.ble._get_system_check_state.side_effect = RuntimeError("invalid handle")
    with (
        patch("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0),
        patch.object(c, "_schedule_reconnect") as reconnect,
    ):
        result = await c._async_update_data()
    assert result["state"]["room"] == 20
    assert c._needs_cache_clear
    reconnect.assert_called_once()


async def test_reconnect_waits_for_owned_io(coordinator):
    c = coordinator
    await c._io_lock.acquire()
    with patch(
        "custom_components.airahome.coordinator.bluetooth.async_ble_device_from_address",
        return_value=Mock(),
    ):
        task = asyncio.create_task(c._async_reconnect())
        await asyncio.sleep(0)
        c.aira.ble._disconnect.assert_not_awaited()
        c._io_lock.release()
        await task
    c.aira.ble._disconnect.assert_awaited_once()
    assert c._is_connected
    assert c._reconnect_attempts == 1


async def test_long_reconnect_counts_one_attempt(coordinator):
    c = coordinator
    gate = asyncio.Event()
    c.aira.ble._connect_device.side_effect = lambda *a, **k: None

    async def connect(*a, **k):
        await gate.wait()
        return True

    c.aira.ble._connect_device.side_effect = connect
    with patch(
        "custom_components.airahome.coordinator.bluetooth.async_ble_device_from_address",
        return_value=Mock(),
    ):
        c._schedule_reconnect()
        for _ in range(5):
            c._schedule_reconnect()
        await asyncio.sleep(0.6)
        assert c._reconnect_attempts == 1
        gate.set()
        await c._reconnect_task
    assert c._reconnect_attempts == 1


async def test_command_requires_terminal_ack_and_new_state(coordinator):
    c = coordinator
    c.data = {"connected": True, "state": {"room": 20}}
    c._channel_good["state"] = perf_counter()

    async def execute(**kwargs):
        async def progress():
            yield {"succeeded": {}}

        return progress()

    c.aira.ble._run_command = AsyncMock(side_effect=execute)
    c.async_refresh = AsyncMock()
    with pytest.raises(HomeAssistantError, match="readback"):
        await c.async_execute_command(Mock())
    with pytest.raises(ServiceValidationError):
        await c.async_execute_command(Mock())
    assert c.aira.ble._run_command.await_count == 1


async def test_shutdown_drains_command_before_transport_cleanup(coordinator):
    c = coordinator
    await c._io_lock.acquire()
    shutdown = asyncio.create_task(c.async_shutdown())
    await asyncio.sleep(0)
    assert c._closing and not shutdown.done()
    c._io_lock.release()
    await shutdown
    assert not c._is_connected
    with pytest.raises(ServiceValidationError):
        await c.async_execute_command(Mock())


@pytest.mark.parametrize("prime_cooldown", [False, True])
async def test_real_refresh_awaits_both_mode_readbacks(
    runtime, entry, monkeypatch, prime_cooldown
):
    from custom_components.airahome.select import AiraPumpMode

    c = runtime["coordinator"]
    c.async_refresh = AiraDataUpdateCoordinator.async_refresh.__get__(c)
    state = {
        "configured_pump_modes": "PUMP_MODE_STATE_HEATING_COOLING",
        "allowed_pump_mode_state": "PUMP_MODE_STATE_HEATING",
    }
    c.data = {"connected": True, "state": state}
    c.aira.ble._get_states = AsyncMock(return_value={"state": state})
    c.aira.ble._get_system_check_state = AsyncMock(
        return_value={"system_check_state": {"errors": []}}
    )
    monkeypatch.setattr("custom_components.airahome.coordinator.BLE_COMMAND_SLEEP", 0)
    if prime_cooldown:
        await c.async_request_refresh()
    reads_before = c.aira.ble._get_states.await_count
    control = AiraPumpMode(c, entry)
    await control.async_select_option("cooling")
    assert c.aira.ble._run_command.await_count == 2
    assert c.aira.ble._get_states.await_count == reads_before + 2
    assert not c._command_needs_readback
    assert not c._debounced_refresh._execute_at_end_of_timer


@pytest.mark.parametrize("failed_state", [False, True])
async def test_filtered_power_requires_classification_freshness(
    runtime, entry, failed_state
):
    from custom_components.airahome.sensor import AiraPowerSensor

    c = runtime["coordinator"]
    c.data = {
        "connected": True,
        "state": {"pump_active_state": "PUMP_ACTIVE_STATE_DHW"},
        "system_check_state": {
            "energy_calculation": {"current_electrical_power_w": 2000}
        },
    }
    c._channel_good = {
        "state": perf_counter() - (0 if failed_state else 700),
        "system_check_state": perf_counter(),
    }
    if failed_state:
        c._channel_failed.add("state")
    filtered = AiraPowerSensor(
        c,
        entry,
        "dhw_test",
        ("system_check_state", "energy_calculation", "current_electrical_power_w"),
        allowed_status=["PUMP_ACTIVE_STATE_DHW"],
    )
    total = AiraPowerSensor(
        c,
        entry,
        "total_test",
        ("system_check_state", "energy_calculation", "current_electrical_power_w"),
    )
    assert not filtered.available
    assert total.available and total.native_value == 2.0
    c._channel_failed.clear()
    c._channel_good["state"] = perf_counter()
    assert filtered.available
