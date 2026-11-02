"""Read reported hot water boost timing without device requests."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


def _boost_end_times(data: dict[str, Any]) -> list[datetime] | None:
    """Read a complete schedule list; missing or malformed data stays unknown."""
    ends = []
    try:
        schedules = data["state"]["scheduler"]["schedules"]
        if not isinstance(schedules, list):
            return None
        for schedule in schedules:
            if not isinstance(schedule, dict) or not schedule:
                return None
            if "boost" not in schedule:
                continue
            events = schedule["boost"]["events"]
            if not isinstance(events, list):
                return None
            for event in events:
                active = event["active"]
                if not isinstance(active, bool):
                    return None
                if not active:
                    continue
                end = event.get("event_end_dt")
                if isinstance(end, str):
                    end = dt_util.parse_datetime(end)
                if not isinstance(end, datetime):
                    return None
                # The SDK's schedule dates are local wall time when no offset is supplied.
                if end.tzinfo is None:
                    end = end.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
                ends.append(dt_util.as_utc(end))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return ends


def boost_end_time(data: dict[str, Any]) -> datetime | None:
    """Return the latest reported active boost end in UTC, if known."""
    ends = _boost_end_times(data)
    return max(ends, default=None) if ends is not None else None


def boost_active(data: dict[str, Any]) -> bool | None:
    """Return whether a reported boost has time left, or None if unknown."""
    ends = _boost_end_times(data)
    if ends is None:
        return None
    now = dt_util.utcnow()
    return any(end > now for end in ends)
