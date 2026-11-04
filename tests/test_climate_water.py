"""Zone scope and confirmed DHW targets using real HA entities, offline BLE."""
import asyncio
from copy import deepcopy
from time import perf_counter
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pyairahome.commands import (
    DisableCoolingFunction,
    DisableHeatingFunction,
    EnableCoolingFunction,
    EnableHeatingFunction,
    SetTargetHotWaterTemperature,
    SetZoneSetpoints,
)

from custom_components.airahome import climate, select, water_heater


@pytest.fixture
def reported_pump(runtime):
    """Supply explicit STATE responses; successful ACK alone never changes state."""
    coordinator = runtime["coordinator"]
    state = {
        "configured_pump_modes": "PUMP_MODE_STATE_HEATING_COOLING",
        "allowed_pump_mode_state": "PUMP_MODE_STATE_HEATING",
        "zone_setpoints_heating": {"zone_1": 20.0, "zone_2": 21.0},
        "target_hot_water_temperature": 50,
        "scheduled_hot_water_temperature": 50,
        "scheduler": {"active_actions": []},
    }

    def report(new_state):
        coordinator._is_connected = True
        coordinator._channel_good.update(state=perf_counter(), system_check_state=perf_counter())
        coordinator._channel_failed.clear()
        coordinator._command_needs_readback = False
        coordinator.async_set_updated_data({"connected": True, "state": deepcopy(new_state)})

    report(state)

    async def refresh():
        report(coordinator.data["state"])

    coordinator.async_refresh = AsyncMock(side_effect=refresh)
    return report


def sent_commands(runtime):
    return [call.kwargs["command_in"] for call in runtime["aira"].ble._run_command.await_args_list]


@pytest.mark.parametrize(("configured", "expected"), [
    ("PUMP_MODE_STATE_HEATING", HVACMode.HEAT),
    ("PUMP_MODE_STATE_HEATING_COOLING", HVACMode.HEAT_COOL),
])
async def test_zone_recovers_configuration_after_partial_startup(
    hass, runtime, reported_pump, add_platform, configured, expected,
):
    coordinator = runtime["coordinator"]
    coordinator._channel_good.pop("state")
    coordinator._channel_failed.add("state")
    coordinator.async_set_updated_data({
        "connected": True, "state": {}, "system_check_state": {"errors": []},
    })
    zones = await add_platform(climate)
    identities = {key: (zone.unique_id, zone.entity_id) for key, zone in zones.items()}
    for zone in zones.values():
        assert not zone.available
        assert zone.hvac_mode is None
        assert hass.states.get(zone.entity_id).state == "unavailable"

    state = {
        "configured_pump_modes": configured,
        "allowed_pump_mode_state": "PUMP_MODE_STATE_HEATING_COOLING",
        "zone_setpoints_heating": {"zone_1": 20.0, "zone_2": 21.0},
    }
    reported_pump(state)
    for key, zone in zones.items():
        assert (zone.unique_id, zone.entity_id) == identities[key]
        assert zone.available
        assert zone.hvac_mode == expected
        assert hass.states.get(zone.entity_id).state == expected
        assert zone.target_temperature is not None
        assert zone.supported_features == ClimateEntityFeature.TARGET_TEMPERATURE
        assert zone.hvac_modes == []

    # Later configuration changes also use telemetry, not the first recovered value.
    state["configured_pump_modes"] = "PUMP_MODE_STATE_COOLING"
    reported_pump(state)
    assert all(zone.hvac_mode == HVACMode.COOL for zone in zones.values())
    coordinator._channel_failed.add("state")
    coordinator.async_update_listeners()
    assert all(zone.hvac_mode is None and not zone.available for zone in zones.values())
    for unknown in (None, "", "PUMP_MODE_STATE_UNSPECIFIED"):
        state["configured_pump_modes"] = unknown
        reported_pump(state)
        assert all(zone.hvac_mode is None for zone in zones.values())
        assert all(hass.states.get(zone.entity_id).state == "unknown" for zone in zones.values())
    assert sent_commands(runtime) == []


async def test_zone_controls_have_no_plant_mode_writes(runtime, reported_pump, add_platform):
    zones = await add_platform(climate)
    assert "zone_1_climate" in zones
    for entity in zones.values():
        assert entity.unique_id.startswith("test-pump_zone_")
        assert entity.supported_features == ClimateEntityFeature.TARGET_TEMPERATURE
        assert entity.hvac_modes == []
        assert entity.hvac_mode == HVACMode.HEAT
        for method in (
            entity.async_turn_on,
            entity.async_turn_off,
            lambda: entity.async_set_hvac_mode(HVACMode.OFF),
            lambda: entity.async_handle_set_hvac_mode_service(HVACMode.HEAT),
        ):
            with pytest.raises(ServiceValidationError):
                await method()
    assert sent_commands(runtime) == []


async def test_zone_temperature_uses_command_helper_and_actual_readback(
    hass, runtime, reported_pump, add_platform,
):
    zone = (await add_platform(climate))["zone_1_climate"]
    coordinator = runtime["coordinator"]

    async def refresh():
        # Readback may lag an ACK: never invent the requested 22 degrees.
        assert zone.target_temperature == 20
        report = deepcopy(coordinator.data["state"])
        report["zone_setpoints_heating"]["zone_1"] = 21.5
        reported_pump(report)

    coordinator.async_refresh.side_effect = refresh
    await zone.async_set_temperature(temperature=22)
    command, = sent_commands(runtime)
    assert isinstance(command, SetZoneSetpoints)
    assert command.to_message().zone_setpoints.zone_1 == 22
    assert zone.target_temperature == 21.5
    assert hass.states.get(zone.entity_id).attributes["temperature"] == 21.5


@pytest.mark.parametrize("temperature", [9, 31, float("nan"), float("inf")])
async def test_zone_rejects_out_of_range(runtime, reported_pump, add_platform, temperature):
    zone = (await add_platform(climate))["zone_1_climate"]
    with pytest.raises(ServiceValidationError):
        await zone.async_set_temperature(temperature=temperature)
    assert sent_commands(runtime) == []


async def test_global_select_setup_does_not_actuate_or_replace_pending_control(
    hass, entry, runtime, reported_pump, add_platform,
):
    entities = await add_platform(select, {"dhw_boost_duration": "6", "pump_mode": "off"})
    assert set(entities) == {"dhw_boost_duration", "pump_mode"}
    assert entities["dhw_boost_duration"].current_option == "6"
    pump = entities["pump_mode"]
    assert pump.name == "Whole pump heating/cooling mode"
    assert pump.current_option == "heating"
    registered = er.async_get(hass).async_get(pump.entity_id)
    assert registered.unique_id == "test-pump_pump_mode"
    assert registered.config_entry_id == entry.entry_id
    assert sent_commands(runtime) == []


@pytest.mark.parametrize(("configured", "options", "commands"), [
    ("PUMP_MODE_STATE_HEATING", ["off", "heating"], [DisableHeatingFunction]),
    ("PUMP_MODE_STATE_COOLING", ["off", "cooling"], [DisableCoolingFunction]),
    ("PUMP_MODE_STATE_HEATING_COOLING", ["off", "heating", "cooling", "heating_and_cooling"],
     [DisableHeatingFunction, DisableCoolingFunction]),
    ("PUMP_MODE_STATE_UNSPECIFIED", [], []),
    (None, [], []),
])
async def test_global_options_follow_configuration(
    runtime, reported_pump, add_platform, configured, options, commands,
):
    runtime["coordinator"].data["state"]["configured_pump_modes"] = configured
    pump = (await add_platform(select))["pump_mode"]
    assert pump.options == options
    if options:
        await pump.async_select_option("off")
    else:
        assert not pump.available
        with pytest.raises(ServiceValidationError):
            await pump.async_select_option("off")
    assert [type(command) for command in sent_commands(runtime)] == commands


async def test_global_selection_is_explicit_not_toggle(runtime, reported_pump, add_platform):
    coordinator = runtime["coordinator"]
    coordinator.data["state"]["configured_pump_modes"] = "PUMP_MODE_STATE_HEATING"
    pump = (await add_platform(select))["pump_mode"]
    for _ in range(2):
        await pump.async_select_option("heating")
    assert [type(command) for command in sent_commands(runtime)] == [EnableHeatingFunction] * 2
    assert pump.current_option == "heating"
    with pytest.raises(ServiceValidationError):
        await pump.async_select_option("cooling")


async def test_global_mode_rechecks_configuration_under_lock(runtime, reported_pump, add_platform):
    coordinator = runtime["coordinator"]
    pump = (await add_platform(select))["pump_mode"]
    async with coordinator._io_lock:
        task = asyncio.create_task(pump.async_select_option("cooling"))
        await asyncio.sleep(0)
        coordinator.data["state"]["configured_pump_modes"] = "PUMP_MODE_STATE_HEATING"
    with pytest.raises(ServiceValidationError):
        await task
    assert sent_commands(runtime) == []


async def test_global_partial_failure_reports_only_actual_state(runtime, reported_pump, add_platform):
    coordinator = runtime["coordinator"]
    pump = (await add_platform(select))["pump_mode"]

    async def progress(**kwargs):
        async def results():
            if isinstance(kwargs["command_in"], DisableHeatingFunction):
                yield {"succeeded": {}}
            else:
                yield {"error": {}}
        return results()

    async def refresh():
        state = deepcopy(coordinator.data["state"])
        state["allowed_pump_mode_state"] = "PUMP_MODE_STATE_IDLE"
        reported_pump(state)

    runtime["aira"].ble._run_command.side_effect = progress
    coordinator.async_refresh.side_effect = refresh
    with pytest.raises(HomeAssistantError):
        await pump.async_select_option("cooling")
    assert [type(command) for command in sent_commands(runtime)] == [DisableHeatingFunction, EnableCoolingFunction]
    assert pump.current_option == "off"
    assert not pump.available


async def test_dhw_target_uses_actual_state_not_stale_derived_value(runtime, reported_pump, add_platform):
    entity = (await add_platform(water_heater))["water_heater"]
    assert entity.unique_id == "test-pump_water_heater"
    runtime["coordinator"].data["state"]["target_hot_water_temperature"] = 55
    assert entity.target_temperature == 55
    runtime["coordinator"].data["state"]["scheduler"]["active_actions"] = [
        {"set_dhw_setpoint": {"temperature": 15}},
    ]
    assert entity.target_temperature == 15
    with pytest.raises(ServiceValidationError) as exc:
        await entity.async_set_temperature(temperature=65)
    assert exc.value.translation_key == "scheduler_active"
    assert sent_commands(runtime) == []


async def test_dhw_repeated_increment_awaits_readback(hass, runtime, reported_pump, add_platform):
    entity = (await add_platform(water_heater))["water_heater"]
    coordinator = runtime["coordinator"]

    async def refresh():
        command = sent_commands(runtime)[-1]
        assert isinstance(command, SetTargetHotWaterTemperature)
        target = command.to_message().temperature
        assert entity.target_temperature != target
        state = deepcopy(coordinator.data["state"])
        state["target_hot_water_temperature"] = target
        # Keep the legacy derived field deliberately stale.
        reported_pump(state)

    coordinator.async_refresh.side_effect = refresh
    for expected in (55, 65):
        await entity.async_set_temperature(temperature=entity.target_temperature + 0.1)
        assert entity.target_temperature == expected
        assert hass.states.get(entity.entity_id).attributes["temperature"] == expected
    assert [command.to_message().temperature for command in sent_commands(runtime)] == [55, 65]
    await entity.async_set_temperature(temperature=65.1)
    assert len(sent_commands(runtime)) == 2


@pytest.mark.parametrize("change", ["scheduler", "target"])
async def test_dhw_guard_rechecked_after_waiting_for_lock(runtime, reported_pump, add_platform, change):
    entity = (await add_platform(water_heater))["water_heater"]
    coordinator = runtime["coordinator"]
    async with coordinator._io_lock:
        task = asyncio.create_task(entity.async_set_temperature(temperature=50.1))
        await asyncio.sleep(0)
        if change == "scheduler":
            coordinator.data["state"]["scheduler"]["active_actions"] = [{"set_dhw_setpoint": {"temperature": 50}}]
        else:
            coordinator.data["state"]["target_hot_water_temperature"] = 55
    with pytest.raises(ServiceValidationError):
        await task
    assert sent_commands(runtime) == []


async def test_concurrent_dhw_increment_rejects_stale_request(runtime, reported_pump, add_platform):
    entity = (await add_platform(water_heater))["water_heater"]
    coordinator = runtime["coordinator"]
    refreshing, release = asyncio.Event(), asyncio.Event()

    async def refresh():
        refreshing.set()
        await release.wait()
        state = deepcopy(coordinator.data["state"])
        state["target_hot_water_temperature"] = 55
        reported_pump(state)

    coordinator.async_refresh.side_effect = refresh
    first = asyncio.create_task(entity.async_set_temperature(temperature=50.1))
    await refreshing.wait()
    second = asyncio.create_task(entity.async_set_temperature(temperature=50.1))
    await asyncio.sleep(0)
    release.set()
    await first
    with pytest.raises(ServiceValidationError):
        await second
    assert len(sent_commands(runtime)) == 1
    assert entity.target_temperature == 55


async def test_dhw_ack_without_matching_readback_is_not_confirmation(runtime, reported_pump, add_platform):
    entity = (await add_platform(water_heater))["water_heater"]
    before = deepcopy(runtime["coordinator"].data)
    with pytest.raises(HomeAssistantError, match="readback is unconfirmed"):
        await entity.async_set_temperature(temperature=55)
    assert entity.target_temperature == 50
    assert runtime["coordinator"].data == before
    assert len(sent_commands(runtime)) == 1


@pytest.mark.parametrize("module,key,kwargs", [
    (climate, "zone_1_climate", {"temperature": 22}),
    (water_heater, "water_heater", {"temperature": 55}),
    (select, "pump_mode", {"option": "off"}),
])
@pytest.mark.parametrize("ack", [None, {"error": {}}, {"succeeded": {}, "error": {}}])
async def test_bad_ack_never_changes_reported_target(
    runtime, reported_pump, add_platform, module, key, kwargs, ack,
):
    entity = (await add_platform(module))[key]
    before = deepcopy(runtime["coordinator"].data)

    async def command(**kwargs):
        async def progress():
            if ack is not None:
                yield ack
        return progress()

    runtime["aira"].ble._run_command.side_effect = command
    method = entity.async_select_option if module is select else entity.async_set_temperature
    with pytest.raises(HomeAssistantError):
        await method(**kwargs)
    assert runtime["coordinator"].data == before
    runtime["coordinator"].async_refresh.assert_not_awaited()
    assert len(sent_commands(runtime)) == 1


async def test_state_channel_availability(runtime, reported_pump, add_platform):
    entities = [
        (await add_platform(climate))["zone_1_climate"],
        (await add_platform(water_heater))["water_heater"],
        (await add_platform(select))["pump_mode"],
    ]
    coordinator = runtime["coordinator"]
    coordinator._channel_failed.add("system_check_state")
    assert all(entity.available for entity in entities)
    coordinator._channel_failed.add("state")
    assert not any(entity.available for entity in entities)
