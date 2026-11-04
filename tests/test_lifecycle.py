"""Offline lifecycle ownership with real HA coordinators and a fake BLE transport."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import airahome
from custom_components.airahome import config_flow
from custom_components.airahome.const import CONF_NUM_PHASES, DOMAIN


@pytest_asyncio.fixture
async def lifecycle(hass, entry, monkeypatch):
    """Patch transport boundaries, retaining actual HA coordinator shutdown."""
    events = []
    coordinators = []
    reconnect_tasks = []
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    aira = SimpleNamespace(ble=SimpleNamespace(), connected=False)

    async def connect(*args, **kwargs):
        events.append("connect")
        aira.connected = True
        return True

    async def cleanup():
        events.append("ble_cleanup")
        for coordinator, task in zip(coordinators, reconnect_tasks):
            assert coordinator._shutdown_requested
            assert task.done()
        aira.connected = False

    aira.ble.add_certificate = Mock()
    aira.ble._connect_device = AsyncMock(side_effect=connect)
    aira.ble._cleanup = AsyncMock(side_effect=cleanup)
    aira.ble._get_configuration = AsyncMock(
        return_value={"config": {"outdoor_unit_size": "12_KW"}}
    )
    aira.ble._get_system_check_state = AsyncMock(
        return_value={"system_check_state": {"energy_calculation": {"voltage_phase_1": 230}}}
    )
    monkeypatch.setattr(airahome, "AiraHome", Mock(return_value=aira))
    monkeypatch.setattr(
        airahome.bluetooth, "async_ble_device_from_address", Mock(return_value=Mock())
    )
    forward = AsyncMock()
    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    monkeypatch.setattr(config_flow, "BLE_COMMAND_SLEEP", 0)
    coordinator_class = airahome.AiraDataUpdateCoordinator

    def create_coordinator(*args):
        coordinator = coordinator_class(*args)
        coordinator.update_interval = None
        coordinator._async_update_data = AsyncMock(
            return_value={"connected": True, "state": {}, "system_check_state": {}}
        )
        coordinator._reconnect_task = asyncio.create_task(asyncio.Event().wait())
        reconnect_tasks.append(coordinator._reconnect_task)
        shutdown = coordinator.async_shutdown

        async def stop():
            events.append("shutdown")
            await shutdown()

        coordinator.async_shutdown = AsyncMock(side_effect=stop)
        coordinators.append(coordinator)
        return coordinator

    factory = Mock(side_effect=create_coordinator)
    monkeypatch.setattr(airahome, "AiraDataUpdateCoordinator", factory)
    runtime = SimpleNamespace(
        aira=aira, coordinators=coordinators, reconnect_tasks=reconnect_tasks,
        events=events, factory=factory,
        forward=forward, unload=unload,
    )
    yield runtime
    for coordinator in coordinators:
        await coordinator.async_shutdown()


def assert_released(hass, entry, lifecycle):
    """Background work is stopped before BLE ownership is released."""
    assert entry.entry_id not in hass.data[DOMAIN]
    lifecycle.aira.ble._cleanup.assert_awaited_once()
    assert not lifecycle.aira.connected
    if lifecycle.coordinators:
        coordinator = lifecycle.coordinators[0]
        assert coordinator._shutdown_requested
        assert lifecycle.reconnect_tasks[0].done()
        assert lifecycle.events.index("shutdown") < lifecycle.events.index("ble_cleanup")


async def test_first_refresh_failure_releases_connected_session(hass, entry, lifecycle):
    create = lifecycle.factory.side_effect

    def fail_refresh(*args):
        coordinator = create(*args)
        coordinator._async_update_data.side_effect = UpdateFailed("synthetic read failure")
        return coordinator

    lifecycle.factory.side_effect = fail_refresh
    with pytest.raises(ConfigEntryNotReady):
        await airahome.async_setup_entry(hass, entry)
    assert_released(hass, entry, lifecycle)
    lifecycle.forward.assert_not_awaited()


async def test_first_refresh_timeout_releases_connected_session(hass, entry, lifecycle):
    create = lifecycle.factory.side_effect

    def timeout_refresh(*args):
        coordinator = create(*args)
        coordinator.async_config_entry_first_refresh = AsyncMock(side_effect=TimeoutError)
        return coordinator

    lifecycle.factory.side_effect = timeout_refresh
    with pytest.raises(ConfigEntryNotReady, match="Timed out"):
        await airahome.async_setup_entry(hass, entry)
    assert_released(hass, entry, lifecycle)


@pytest.mark.parametrize("stage", ["connect", "refresh", "forward"])
async def test_setup_cancellation_unwinds_before_propagating(hass, entry, lifecycle, stage):
    started = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    if stage == "connect":
        lifecycle.aira.ble._connect_device.side_effect = block
    elif stage == "forward":
        lifecycle.forward.side_effect = block
    else:
        create = lifecycle.factory.side_effect

        def block_refresh(*args):
            coordinator = create(*args)
            coordinator._async_update_data.side_effect = block
            return coordinator

        lifecycle.factory.side_effect = block_refresh

    task = asyncio.create_task(airahome.async_setup_entry(hass, entry))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_released(hass, entry, lifecycle)


async def test_forward_failure_removes_published_runtime(hass, entry, lifecycle):
    lifecycle.forward.side_effect = RuntimeError("synthetic platform setup failure")
    with pytest.raises(RuntimeError, match="platform setup failure"):
        await airahome.async_setup_entry(hass, entry)
    assert_released(hass, entry, lifecycle)


async def test_connection_failure_cleans_partial_session(hass, entry, lifecycle, monkeypatch):
    monkeypatch.setattr(
        airahome, "connect_with_cache_retry",
        AsyncMock(side_effect=RuntimeError("synthetic connection failure")),
    )
    with pytest.raises(ConfigEntryNotReady):
        await airahome.async_setup_entry(hass, entry)
    assert_released(hass, entry, lifecycle)
    lifecycle.factory.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("synthetic cleanup failure")])
async def test_cleanup_error_preserves_setup_failure(hass, entry, lifecycle, error):
    lifecycle.forward.side_effect = ValueError("original setup failure")
    lifecycle.aira.ble._cleanup.side_effect = error
    with pytest.raises(ValueError, match="original setup failure"):
        await airahome.async_setup_entry(hass, entry)
    lifecycle.aira.ble._cleanup.assert_awaited_once()
    assert lifecycle.coordinators[0]._shutdown_requested
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_failed_platform_unload_retains_runtime_and_transport(hass, entry, lifecycle):
    assert await airahome.async_setup_entry(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    lifecycle.unload.return_value = False
    assert not await airahome.async_unload_entry(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id] is runtime
    assert lifecycle.aira.connected
    lifecycle.aira.ble._cleanup.assert_not_awaited()
    lifecycle.coordinators[0].async_shutdown.assert_not_awaited()
    assert not lifecycle.reconnect_tasks[0].done()


@pytest.mark.parametrize("error", [RuntimeError("synthetic unload failure"), asyncio.CancelledError()])
async def test_platform_unload_exception_retains_session(hass, entry, lifecycle, error):
    assert await airahome.async_setup_entry(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    lifecycle.unload.side_effect = error
    with pytest.raises(type(error)):
        await airahome.async_unload_entry(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id] is runtime
    assert lifecycle.aira.connected
    lifecycle.aira.ble._cleanup.assert_not_awaited()
    lifecycle.coordinators[0].async_shutdown.assert_not_awaited()


async def test_successful_unload_stops_coordinator_before_ble(hass, entry, lifecycle):
    assert await airahome.async_setup_entry(hass, entry)
    lifecycle.aira.ble._cleanup.assert_not_awaited()
    assert await airahome.async_unload_entry(hass, entry)
    assert_released(hass, entry, lifecycle)
    # HA may invoke the registered shutdown again after integration unload.
    await entry._async_process_on_unload(hass)
    assert lifecycle.coordinators[0].async_shutdown.await_count == 2
    assert lifecycle.coordinators[0]._closing
    assert not lifecycle.aira.connected
    lifecycle.aira.ble._connect_device.assert_awaited_once()
    lifecycle.aira.ble._cleanup.assert_awaited_once()


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("synthetic cleanup failure")])
async def test_unload_cleanup_error_still_stops_coordinator(hass, entry, lifecycle, error):
    assert await airahome.async_setup_entry(hass, entry)
    lifecycle.aira.ble._cleanup.side_effect = error
    assert await airahome.async_unload_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN]
    assert lifecycle.coordinators[0]._shutdown_requested
    lifecycle.aira.ble._cleanup.assert_awaited_once()


async def test_cancellation_waits_for_cleanup_to_finish(lifecycle):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def cleanup():
        started.set()
        await finish.wait()
        lifecycle.aira.connected = False

    lifecycle.aira.connected = True
    lifecycle.aira.ble._cleanup.side_effect = cleanup
    task = asyncio.create_task(airahome.async_cleanup_resources(lifecycle.aira))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lifecycle.aira.connected


async def test_cancellation_during_shutdown_waits_before_ble(hass, entry, lifecycle):
    coordinator = lifecycle.factory(hass, entry, lifecycle.aira)
    shutdown = coordinator.async_shutdown
    started = asyncio.Event()
    finish = asyncio.Event()

    async def stop():
        started.set()
        await finish.wait()
        await shutdown()

    coordinator.async_shutdown = AsyncMock(side_effect=stop)
    task = asyncio.create_task(
        airahome.async_cleanup_resources(lifecycle.aira, coordinator)
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    lifecycle.aira.ble._cleanup.assert_not_awaited()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator._shutdown_requested
    assert lifecycle.reconnect_tasks[0].done()
    lifecycle.aira.ble._cleanup.assert_awaited_once()


@pytest.fixture
def flow(hass, lifecycle):
    flow = config_flow.AiraHomeConfigFlow()
    flow.hass = hass
    flow._aira = lifecycle.aira
    return flow


async def test_config_flow_success_awaits_cleanup(flow, lifecycle):
    success, installation = await flow._ble_connect_test("00:11:22:33:44:55")
    assert success
    assert installation == {CONF_NUM_PHASES: 3, "outdoor_unit_size": "12_KW"}
    lifecycle.aira.ble._cleanup.assert_awaited_once()
    assert not lifecycle.aira.connected


@pytest.mark.parametrize("stage", ["connect", "configuration", "system_check"])
async def test_config_flow_failure_always_cleans_up(flow, lifecycle, monkeypatch, stage):
    error = RuntimeError("synthetic validation failure")
    if stage == "connect":
        monkeypatch.setattr(config_flow, "connect_with_cache_retry", AsyncMock(side_effect=error))
    elif stage == "configuration":
        lifecycle.aira.ble._get_configuration.side_effect = error
    else:
        lifecycle.aira.ble._get_system_check_state.side_effect = error
    assert await flow._ble_connect_test("00:11:22:33:44:55") == (
        False, {"error": "cannot_connect"},
    )
    lifecycle.aira.ble._cleanup.assert_awaited_once()
    assert not lifecycle.aira.connected


async def test_config_flow_cancellation_cleans_connected_session(flow, lifecycle):
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.Event().wait()

    lifecycle.aira.ble._get_configuration.side_effect = block
    task = asyncio.create_task(flow._ble_connect_test("00:11:22:33:44:55"))
    await asyncio.wait_for(started.wait(), 2)
    assert lifecycle.aira.connected
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lifecycle.aira.ble._cleanup.assert_awaited_once()
    assert not lifecycle.aira.connected
