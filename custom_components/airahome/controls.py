"""Local pending values for Aira controls."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICE_UUID, DOMAIN
from .services import AWAY_MODE_MIN_DAYS


@dataclass
class AiraPendingControls:
    """Selections applied only when the corresponding button is pressed."""

    away_start_date: date = field(default_factory=lambda: dt_util.now().date())
    away_end_date: date = field(
        default_factory=lambda: dt_util.now().date() + timedelta(days=AWAY_MODE_MIN_DAYS)
    )
    dhw_boost_duration: str = "1"


def control_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Attach controls to the existing heat pump device."""
    return DeviceInfo(identifiers={(DOMAIN, entry.data[CONF_DEVICE_UUID])})
