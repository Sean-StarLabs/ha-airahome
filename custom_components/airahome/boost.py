"""Read reported hot water boost timing without device requests."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


def boost_end_time(data: dict[str, Any]) -> datetime | None:
    """Return the latest active boost end in UTC, or None if it is unknown."""
    ends = []
    try:
        for schedule in data["state"]["scheduler"]["schedules"]:
            if (boost := schedule.get("boost")) is None:
                continue
            for event in boost.get("events", []):
                if event.get("active") is not True:
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
    return max(ends, default=None)
