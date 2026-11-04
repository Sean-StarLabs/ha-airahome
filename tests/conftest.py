"""Offline fixtures using real Home Assistant entities and registries."""
import logging
from datetime import timedelta
from time import perf_counter
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers import (
    frame,
    restore_state,
)
from homeassistant.helpers import (
    label_registry as lr,
)
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util

from custom_components.airahome.const import CONF_DEVICE_UUID, CONF_MAC_ADDRESS, DOMAIN
from custom_components.airahome.controls import AiraPendingControls
from custom_components.airahome.coordinator import AiraDataUpdateCoordinator
from custom_components.airahome.services import async_setup_services


@pytest_asyncio.fixture
async def hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    for registry in (fr, lr, ar, dr, er):
        await registry.async_load(hass)
    await restore_state.async_load(hass, load_empty=True)
    yield hass
    await hass.async_stop()


@pytest.fixture
def entry(hass):
    entry = ConfigEntry(
        domain=DOMAIN, title="Aira", version=1, minor_version=1,
        data={CONF_DEVICE_UUID: "test-pump", CONF_MAC_ADDRESS: "00:11:22:33:44:55"},
        options={}, source="user", unique_id="test-pump",
        discovery_keys=MappingProxyType({}), subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "test-pump")}, name="Aira",
    )
    return entry


@pytest_asyncio.fixture
async def runtime(hass, entry):
    aira = Mock()

    async def success(**kwargs):
        async def progress():
            yield {"received": {}}
            yield {"succeeded": {}}
        return progress()

    aira.ble._run_command = AsyncMock(side_effect=success)
    coordinator = AiraDataUpdateCoordinator(hass, entry, aira)
    coordinator.update_interval = None
    coordinator.async_set_updated_data({"connected": True, "state": {}})
    coordinator._channel_good = {"state": perf_counter(), "system_check_state": perf_counter()}
    async def refresh():
        coordinator._channel_good["state"] = perf_counter()
        coordinator._command_needs_readback = False
    coordinator.async_refresh = AsyncMock(side_effect=refresh)
    data = {
        "aira": aira, "coordinator": coordinator,
        "pending_controls": AiraPendingControls(),
    }
    hass.data[DOMAIN] = {entry.entry_id: data}
    await async_setup_services(hass)
    yield data
    await coordinator.async_shutdown()


@pytest_asyncio.fixture
async def add_platform(hass, entry):
    platforms = []

    async def add(module, restored=None):
        domain = module.__name__.rsplit(".", 1)[-1]
        entities = []
        await module.async_setup_entry(hass, entry, lambda new, *args: entities.extend(new))
        platform = EntityPlatform(
            hass=hass, logger=logging.getLogger(__name__), domain=domain,
            platform_name=DOMAIN, platform=module, scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        platform.config_entry = entry
        platforms.append(platform)
        for entity in entities:
            key = entity.unique_id.removeprefix("test-pump_")
            entity.entity_id = f"{domain}.{key}"
            if restored and key in restored and isinstance(entity, restore_state.RestoreEntity):
                from homeassistant.core import State
                entity.async_get_last_state = AsyncMock(
                    return_value=State(entity.entity_id, restored[key]),
                )
        await platform.async_add_entities(entities)
        assert len(platform.entities) == len(entities)
        return {entity.unique_id.removeprefix("test-pump_"): entity for entity in entities}

    yield add
    for platform in platforms:
        await platform.async_reset()


def active_boost_data():
    """Reported boost with a future end, suitable for cancel commands."""
    return {"connected": True, "state": {"scheduler": {"schedules": [
        {"boost": {"events": [{"active": True, "event_end_dt": dt_util.utcnow() + timedelta(hours=1)}]}},
    ]}}}
