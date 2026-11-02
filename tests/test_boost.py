"""Reported boost timing, unknown data and coordinator availability."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.airahome.binary_sensor import AiraWaterBoostActiveSensor
from custom_components.airahome.sensor import AiraWaterBoostEndSensor


def boost_data(end, active=True):
    return {"connected": True, "state": {"scheduler": {"schedules": [
        {"boost": {"events": [{"active": active, "event_end_dt": end}]}},
    ]}}}


@pytest.mark.parametrize("remaining", [-60, 0, 60])
async def test_boost_expiry(runtime, entry, remaining):
    now = datetime(2030, 6, 1, 12, tzinfo=UTC)
    coordinator = runtime["coordinator"]
    coordinator.data = boost_data(now + timedelta(seconds=remaining))
    sensor = AiraWaterBoostActiveSensor(coordinator, entry)
    with patch("custom_components.airahome.binary_sensor.dt_util.utcnow", return_value=now):
        assert sensor.is_on is (remaining > 0)
    assert sensor.unique_id == "test-pump_dhw_boost_active"
    runtime["aira"].ble._run_command.assert_not_awaited()


@pytest.mark.parametrize("data", [
    {"connected": True, "state": {}},
    {"connected": True, "state": {"scheduler": {"schedules": []}}},
    boost_data(None), boost_data("invalid"), boost_data(123), boost_data(None, False),
])
async def test_unknown_timing(runtime, entry, data):
    runtime["coordinator"].data = data
    assert AiraWaterBoostActiveSensor(runtime["coordinator"], entry).is_on is None
    assert AiraWaterBoostEndSensor(runtime["coordinator"], entry).native_value is None


@pytest.mark.parametrize("end", [
    datetime(2030, 6, 1, 12), "2030-06-01T12:00:00",
    datetime(2030, 6, 1, 12, tzinfo=ZoneInfo("Europe/London")),
    "2030-06-01T12:00:00+01:00",
])
async def test_boost_timezone(runtime, entry, end):
    with patch.object(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/London")):
        runtime["coordinator"].data = boost_data(end)
        sensor = AiraWaterBoostEndSensor(runtime["coordinator"], entry)
        assert sensor.native_value == datetime(2030, 6, 1, 11, tzinfo=UTC)
        assert sensor.unique_id == "test-pump_dhw_boost_end"


@pytest.mark.parametrize(("connected", "success"), [(False, True), (True, False)])
async def test_boost_unavailable(runtime, entry, connected, success):
    coordinator = runtime["coordinator"]
    coordinator.data["connected"] = connected
    coordinator.last_update_success = success
    assert not AiraWaterBoostActiveSensor(coordinator, entry).available
