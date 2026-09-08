"""Independent polling for each REST resource."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DecaidClient, DecaidError

_LOGGER = logging.getLogger(__name__)


class DecaidCoordinator(DataUpdateCoordinator):
    """Fetch one endpoint without coupling its availability to the others."""

    def __init__(self, hass: HomeAssistant, client: DecaidClient, path: str, seconds: int):
        super().__init__(
            hass,
            _LOGGER,
            name=f"Decaid {path}",
            update_interval=timedelta(seconds=seconds),
            always_update=False,
        )
        self.client = client
        self.path = path

    async def _async_update_data(self):
        try:
            data = await self.client.get(self.path)
        except DecaidError as err:
            if self.path == "machine/state":
                self.update_interval = timedelta(seconds=10)
            raise UpdateFailed(str(err)) from err
        if self.path == "machine/state":
            state = data["state"].get("state")
            substate = data["state"].get("substate")
            if state in (None, "", "unknown", "disconnected", "sleeping"):
                seconds = 10
            elif state in ("idle", "schedIdle") and substate in (None, "idle"):
                # DE1 may remain idle during warm-up; keep temperatures responsive.
                seconds = 2
            else:
                seconds = 1
            self.update_interval = timedelta(seconds=seconds)
        return data


@dataclass
class DecaidData:
    """Runtime data owned by a config entry."""

    client: DecaidClient
    machine: DecaidCoordinator
    workflow: DecaidCoordinator
    settings: DecaidCoordinator
    devices: DecaidCoordinator

    def machine_connected(self, machine_id: str | None) -> bool:
        return self.devices.last_update_success and any(
            device.get("type") == "machine"
            and (not machine_id or device.get("id") == machine_id)
            and device.get("state") == "connected"
            for device in (self.devices.data or [])
        )
