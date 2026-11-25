"""Diagnostics download, with network and device identifiers redacted."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_MACHINE_ID

REDACTED = "**REDACTED**"
# Device names, IDs, and addresses identify a household's hardware.
TO_REDACT = {CONF_HOST, CONF_MACHINE_ID, "id", "name", "address", "mac", "serial", "serialNumber"}
COORDINATORS = (
    "machine",
    "workflow",
    "settings",
    "devices",
    "water",
    "scale",
    "shot",
    "shot_settings",
    "shots",
    "machine_info",
    "capabilities",
    "machine_settings",
    "display",
    "presence",
    "app_update",
    "firmware",
    "cup_warmer",
    "preheat",
)


def _summarize(name: str, data: Any) -> Any:
    """Keep diagnostics small and free of settings values that may be private."""
    if data is None:
        return None
    if name == "settings":
        # Tablet settings can hold account or network details: keys only.
        return sorted(data) if isinstance(data, dict) else type(data).__name__
    if name == "shots":
        # Shot records name baristas, drinkers, and coffees: counts and fields only.
        if not isinstance(data, dict):
            return None
        latest = data.get("latest")
        return {
            "total": data.get("total"),
            "latest_fields": sorted(latest) if isinstance(latest, dict) else None,
            "summary": data.get("summary"),
        }
    if name == "presence" and isinstance(data, dict):
        return {**data, "schedules": len(data.get("schedules") or [])}
    if name == "firmware" and isinstance(data, dict):
        return {
            **{k: v for k, v in data.items() if k != "artifacts"},
            "artifacts": [
                {k: a.get(k) for k in ("id", "build", "eligibility")}
                for a in data.get("artifacts") or []
                if isinstance(a, dict)
            ],
        }
    return async_redact_data(data, TO_REDACT)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    data = entry.runtime_data
    host = entry.data[CONF_HOST]
    coordinators = {}
    for name in COORDINATORS:
        coordinator = getattr(data, name)
        stream = getattr(coordinator, "stream", None)
        error = coordinator.last_exception
        coordinators[name] = {
            "last_update_success": coordinator.last_update_success,
            "update_interval": (
                coordinator.update_interval.total_seconds() if coordinator.update_interval else None
            ),
            # Connection errors quote the tablet address.
            "last_exception": str(error).replace(host, REDACTED) if error else None,
            "device_connected": getattr(coordinator, "device_connected", None),
            "stream_active": stream.active if stream is not None else None,
            "data": _summarize(name, coordinator.data),
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinators": coordinators,
    }
