"""Data update coordinator for Aira Heat Pump."""

from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from time import perf_counter
from typing import Any
from collections.abc import Callable

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyairahome import AiraHome

from .const import (
    BLE_COMMAND_SLEEP,
    BLE_CONNECT_TIMEOUT,
    BLE_RECONNECT_BACKOFF,
    CACHE_ERROR_PATTERNS,
    CONF_DEVICE_NAME,
    DEFAULT_DATA,
    DEFAULT_SHORT_NAME,
    ERROR_SOURCE_PREFIXES,
    STALE_DATA_THRESHOLD,
)


_LOGGER = logging.getLogger(__name__)


def _parse_error(error: dict[str, Any]) -> tuple[str, str, str, str, Any]:
    """Parse an error returned by the Aira protocol."""

    # default values
    source = "UNKNOWN"
    raw_code = "unknown"
    display_code = "Unknown"

    # Look for the first non-empty error source and code, ignoring unspecified codes
    for src, prefix in ERROR_SOURCE_PREFIXES.items():
        value = error.get(src, "")
        if value and not str(value).endswith("_UNSPECIFIED"):
            source = src.upper()
            raw_code = str(value)
            display_code = raw_code.removeprefix(prefix).replace("_", " ").title()
            break

    severity = (
        str(error.get("severity", "SEVERITY_UNSPECIFIED"))
        .removeprefix("SEVERITY_")
        .title()
    )
    return source, display_code, raw_code, severity, error.get("occurred_at")


def _is_cache_error(err: Exception) -> bool:
    """Attempt to determine if the exception could be caused by a stale GATT cache."""
    error_message = str(err).lower()
    return any(pattern in error_message for pattern in CACHE_ERROR_PATTERNS)


class AiraDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Aira data from BLE."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        aira: AiraHome,
        update_interval: int = 30,
        mac_address: str | None = None,
    ) -> None:
        """Initialise coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=entry.data.get(CONF_DEVICE_NAME, DEFAULT_SHORT_NAME),
            update_interval=timedelta(seconds=update_interval),
        )
        self.config_entry = entry
        self.aira = aira
        self.mac_address = mac_address
        # The initial connection is established before the coordinator is created (in init).
        self._is_connected = True
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_attempts = 0
        # perf_counter timestamp for the next allowed reconnect.
        self._next_reconnect_at = 0.0
        self._needs_cache_clear: bool = False

        # Map raw error codes to their severity and first-seen timestamp.
        self._active_errors: dict[str, tuple[str, str]] = {}

        self._io_lock = asyncio.Lock()
        self._closing = False
        self._reconnecting = False
        self._command_needs_readback = False
        self._channel_data: dict[str, dict[str, Any]] = {}
        self._channel_good: dict[str, float] = {}
        self._channel_failed: set[str] = set()
        self._system_failures = 0
        self._next_system_check = 0.0
        self._channel_error_codes: dict[str, list[dict[str, Any]]] = {}

        # Initialize coordinator data with empty but valid data structure to prevent sensor crashes
        self.data = deepcopy(DEFAULT_DATA)

    async def async_shutdown(self) -> None:
        """Stop admission and wait for owned I/O before transport cleanup."""
        self._closing = True
        await super().async_shutdown()
        task = self._reconnect_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        async with self._io_lock:
            self._is_connected = False

    def channel_available(self, channel: str = "state") -> bool:
        """A cached response is not new evidence that its channel is healthy."""
        timestamp = self._channel_good.get(channel)
        return bool(
            self._is_connected
            and not self._closing
            and not self._reconnecting
            and self.data
            and self.data.get("connected")
            and channel not in self._channel_failed
            and timestamp is not None
            and 0 <= perf_counter() - timestamp < STALE_DATA_THRESHOLD
        )

    @property
    def command_available(self) -> bool:
        return (
            self.channel_available()
            and not self._command_needs_readback
            and self._reconnect_task is None
        )

    async def async_execute_command(
        self, command_in: Any, *, validate: Callable[[], None] | None = None
    ) -> None:
        """Own one command through its terminal ACK; never replay an uncertain write."""
        async with self._io_lock:
            if not self.command_available or not self._is_connected:
                raise ServiceValidationError(
                    "Aira is reconnecting, stale or awaiting readback"
                )
            if validate is not None:
                validate()
            progress = None
            stream = None
            self._command_needs_readback = True
            self.async_update_listeners()
            try:
                async with asyncio.timeout(45):
                    stream = await self.aira.ble._run_command(command_in=command_in)
                    async for progress in stream:
                        pass
                if (
                    not isinstance(progress, dict)
                    or "error" in progress
                    or not isinstance(progress.get("succeeded"), dict)
                ):
                    raise HomeAssistantError("Aira did not confirm the command")
            finally:
                if stream is not None and (close := getattr(stream, "aclose", None)):
                    await close()
        # A read cannot run while we own the transport lock. No optimistic state writes.
        await self.async_refresh()
        if self._command_needs_readback:
            raise HomeAssistantError(
                "Aira command acknowledged; fresh readback unavailable"
            )

    def _calculate_scheduled_hot_water_temperature(
        self, state_dict: dict[str, Any]
    ) -> float | None:
        """Calculate the scheduled hot water temperature from scheduler active actions."""
        try:
            scheduler = state_dict.get("scheduler", {})
            if not scheduler:
                return state_dict.get("target_hot_water_temperature")
            active_actions = scheduler.get("active_actions", [])
            if not active_actions:
                return state_dict.get("target_hot_water_temperature")
            for action in active_actions:
                if "set_dhw_setpoint" in action:
                    dhw_temp = action["set_dhw_setpoint"].get("temperature")
                    if dhw_temp is not None:
                        return float(dhw_temp)
            return state_dict.get("target_hot_water_temperature")
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _response_body(response: Any, channel: str) -> dict[str, Any] | None:
        if not isinstance(response, dict):
            return None
        if (
            response.get("error", "DATA_RESPONSE_ERROR_UNSPECIFIED")
            != "DATA_RESPONSE_ERROR_UNSPECIFIED"
        ):
            return None
        body = response.get(channel)
        if (
            isinstance(body, dict)
            and "errors" in body
            and (
                not isinstance(body["errors"], list)
                or any(not isinstance(x, dict) for x in body["errors"])
            )
        ):
            return None
        return deepcopy(body) if isinstance(body, dict) and body else None

    async def _fetch_all_data(
        self, start_time: float, rssi: int | None
    ) -> dict[str, Any]:
        """Track the two responses independently; failed diagnostics cannot clear faults."""
        received = set()
        for channel, method in (
            ("state", self.aira.ble._get_states),
            ("system_check_state", self.aira.ble._get_system_check_state),
        ):
            if channel == "system_check_state":
                if perf_counter() < self._next_system_check:
                    continue
                await asyncio.sleep(BLE_COMMAND_SLEEP)
            try:
                async with asyncio.timeout(40):
                    response = await method()
                body = self._response_body(response, channel)
                if body is None:
                    raise ValueError("Empty or unsuccessful response")
            except Exception as err:
                self._channel_failed.add(channel)
                if _is_cache_error(err):
                    self._needs_cache_clear = True
                if channel == "system_check_state":
                    self._system_failures += 1
                    self._next_system_check = perf_counter() + min(
                        60 * 2 ** min(self._system_failures - 1, 4), 900
                    )
                _LOGGER.warning("Failed to fetch %s: %s", channel, err)
                continue
            received.add(channel)
            self._channel_failed.discard(channel)
            self._channel_data[channel] = body
            self._channel_good[channel] = perf_counter()
            errors = body.get("errors", [])
            self._channel_error_codes[channel] = (
                [x for x in errors if isinstance(x, dict)]
                if isinstance(errors, list)
                else []
            )
            if channel == "state":
                self._command_needs_readback = False
            else:
                self._system_failures = 0
                self._next_system_check = 0.0
        if received and not self._needs_cache_clear:
            self._reconnect_attempts = 0
            self._next_reconnect_at = 0.0
        if not received and not (
            "state" in self._channel_data and "state" not in self._channel_failed
        ):
            raise UpdateFailed("No fresh Aira response")
        state = deepcopy(self._channel_data.get("state", {}))
        state["scheduled_hot_water_temperature"] = (
            self._calculate_scheduled_hot_water_temperature(state)
        )
        self._handle_error_changes(
            [error for errors in self._channel_error_codes.values() for error in errors]
        )
        return {
            "state": state,
            "system_check_state": deepcopy(
                self._channel_data.get("system_check_state", {})
            ),
            "connected": True,
            "rssi": rssi,
        }

    def _handle_error_changes(self, errors: list[dict[str, Any]]) -> None:
        """Log errors that appeared or cleared since the previous update."""
        device_name = self.config_entry.data.get(CONF_DEVICE_NAME, DEFAULT_SHORT_NAME)
        current_codes: set[str] = set()
        parsed: dict[str, tuple[str, str, str, Any]] = {}

        for error in errors:
            source, display_code, raw_code, severity, occurred_at = _parse_error(error)
            if raw_code != "unknown":
                current_codes.add(raw_code)
                parsed[raw_code] = (source, display_code, severity, occurred_at)

        for raw_code in current_codes - set(self._active_errors):
            source, display_code, severity, occurred_at = parsed[raw_code]
            if occurred_at:
                first_seen_at = (
                    occurred_at.isoformat()
                    if hasattr(occurred_at, "isoformat")
                    else str(occurred_at)
                )
            else:
                first_seen_at = datetime.now(timezone.utc).isoformat()
            self._active_errors[raw_code] = (severity, first_seen_at)

            if severity.lower() in {"critical", "error"}:
                _LOGGER.error(
                    "Aira alarm active on %s: [%s] %s (severity: %s, first_seen_at: %s)",
                    device_name,
                    source,
                    display_code,
                    severity,
                    first_seen_at,
                )
            else:
                previous_level = _LOGGER.level
                try:
                    _LOGGER.setLevel(logging.WARNING)
                    _LOGGER.warning(
                        "Aira warning active on %s: [%s] %s (severity: %s, first_seen_at: %s)",
                        device_name,
                        source,
                        display_code,
                        severity,
                        first_seen_at,
                    )
                finally:
                    _LOGGER.setLevel(previous_level)

        cleared = set(self._active_errors) - current_codes
        for raw_code in cleared:
            prefix = next(
                (
                    prefix
                    for prefix in ERROR_SOURCE_PREFIXES.values()
                    if raw_code.startswith(prefix)
                ),
                "",
            )
            display_name = raw_code.removeprefix(prefix).replace("_", " ").title()
            severity, _ = self._active_errors.pop(raw_code)

            if severity.lower() in {"critical", "error"}:
                _LOGGER.error(
                    "Aira alarm cleared on %s: %s",
                    device_name,
                    display_name,
                )
            else:
                previous_level = _LOGGER.level
                try:
                    _LOGGER.setLevel(logging.WARNING)
                    _LOGGER.warning(
                        "Aira warning cleared on %s: %s",
                        device_name,
                        display_name,
                    )
                finally:
                    _LOGGER.setLevel(previous_level)

    async def _async_clear_gatt_cache(self) -> bool:
        if not self.mac_address:
            return False
        try:
            async with asyncio.timeout(10):
                cleared = await self.aira.ble._clear_cache(self.mac_address)
        except Exception:
            _LOGGER.warning("Aira GATT cache clear failed")
            return False
        if cleared:
            self._needs_cache_clear = False
        return bool(cleared)

    async def _async_reconnect(self) -> None:
        """Connection replacement and normal I/O share one owner."""
        try:
            async with self._io_lock:
                if self._closing:
                    return
                self._reconnecting = True
                self._is_connected = False
                self._reconnect_attempts += 1
                delay = BLE_RECONNECT_BACKOFF[
                    min(self._reconnect_attempts - 1, len(BLE_RECONNECT_BACKOFF) - 1)
                ]
                self._next_reconnect_at = perf_counter() + delay
                self.async_update_listeners()
                try:
                    if self._needs_cache_clear:
                        await self._async_clear_gatt_cache()
                    async with asyncio.timeout(45):
                        await self.aira.ble._disconnect()
                        await asyncio.sleep(0.5)
                        device = bluetooth.async_ble_device_from_address(
                            self.hass, self.mac_address, connectable=True
                        )
                        if device is None:
                            return
                        self._is_connected = bool(
                            await self.aira.ble._connect_device(
                                device, timeout=BLE_CONNECT_TIMEOUT
                            )
                        )
                except Exception as err:
                    if _is_cache_error(err):
                        self._needs_cache_clear = True
                    _LOGGER.warning("Aira reconnect failed: %s", err)
                finally:
                    self._reconnecting = False
                    self.async_update_listeners()
        finally:
            self._reconnect_task = None

    def _schedule_reconnect(self) -> None:
        if (
            self._closing
            or self._reconnect_task is not None
            or perf_counter() < self._next_reconnect_at
        ):
            return
        self._reconnect_task = self.hass.async_create_background_task(
            self._async_reconnect(), "Aira BLE reconnect"
        )

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._io_lock:
            if self._closing:
                raise UpdateFailed("Aira is closing")
            rssi = None
            with contextlib.suppress(Exception):
                info = bluetooth.async_last_service_info(
                    self.hass, self.mac_address, connectable=True
                )
                if info is not None:
                    rssi = info.rssi
            if self._is_connected:
                try:
                    data = await self._fetch_all_data(perf_counter(), rssi)
                except UpdateFailed:
                    self._is_connected = False
                else:
                    if self._needs_cache_clear:
                        # A real GATT error needs one paced recovery, even after partial success.
                        self._schedule_reconnect()
                    return data
            self._schedule_reconnect()
            raise UpdateFailed("Aira disconnected; reconnect scheduled")
