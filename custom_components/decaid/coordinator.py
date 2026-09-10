"""Push telemetry with independent REST polling and fallback."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DecaidClient, DecaidError
from .stream import STALE_SECONDS, DecaidStream

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


class DecaidPushCoordinator(DecaidCoordinator):
    """Stream first, with adaptive REST polling whenever the stream is stale."""

    def __init__(self, hass, client, path, seconds, stream_path):
        super().__init__(hass, client, path, seconds)
        self.stream = DecaidStream(self, stream_path)
        self.machine_connected = True
        self._latest = None
        self._revision = 0
        self._connection_generation = 0
        self._published_at = 0.0
        self._cancel_publish = None
        self._stopped = False

    def start(self, entry):
        self.stream.start(entry)

    async def stop(self):
        self._stopped = True
        self._cancel_pending_publish()
        await self.stream.stop()

    @callback
    def _cancel_pending_publish(self):
        if self._cancel_publish is not None:
            self._cancel_publish()
            self._cancel_publish = None

    @callback
    def set_machine_connected(self, connected):
        if connected == self.machine_connected:
            return
        self.machine_connected = connected
        self._connection_generation += 1
        self._latest = None
        self._revision += 1
        self._cancel_pending_publish()
        if not connected:
            self.async_set_update_error(UpdateFailed("Machine disconnected"))

    @callback
    def async_receive(self, data):
        """Remember every frame, publish state changes immediately and numbers at 1 Hz."""
        if self._stopped or not self.machine_connected:
            return
        self._latest = data
        self._revision += 1
        state_changed = self.path == "machine/state" and (
            not self.data or data["state"] != self.data.get("state")
        )
        elapsed = monotonic() - self._published_at
        if self.path == "devices" or state_changed or not self.last_update_success or elapsed >= 1:
            self._publish()
        elif self._cancel_publish is None:
            self._cancel_publish = async_call_later(self.hass, 1 - elapsed, self._publish)

    @callback
    def _publish(self, _now=None):
        self._cancel_pending_publish()
        if self._stopped or self._latest is None or not self.machine_connected:
            return
        self._published_at = monotonic()
        # Changed pushes postpone this watchdog. Unchanged fresh frames satisfy
        # it from the cache; a silent machine falls back to REST.
        self.update_interval = timedelta(seconds=60 if self.path == "devices" else STALE_SECONDS)
        if self.last_update_success and self._latest == self.data:
            return
        self.async_set_updated_data(self._latest)

    @callback
    def async_stream_lost(self):
        """Cancel queued publications before closing a broken socket."""
        # Retain the last frame for revision comparisons with in-flight REST.
        # stream.active is false, so it cannot satisfy a new refresh request.
        self._cancel_pending_publish()
        self.update_interval = timedelta(seconds=10 if self.path == "machine/state" else 60)

    async def async_stream_failed(self):
        self.async_stream_lost()
        if not self._stopped:
            await self.async_refresh()

    async def _async_update_data(self):
        if not self.machine_connected:
            self.update_interval = timedelta(seconds=10)
            raise UpdateFailed("Machine disconnected")
        if (
            self.stream.active
            and self._latest is not None
            and (self.path == "devices" or monotonic() - self.stream.received_at <= 2)
        ):
            # Fresh push data also satisfies command preflight reads.
            return self._latest
        revision = self._revision
        connection_generation = self._connection_generation
        try:
            data = await super()._async_update_data()
        except UpdateFailed:
            if self._revision == revision or self._latest is None or not self.stream.active:
                raise
            data = self._latest
        if not self.machine_connected:
            raise UpdateFailed("Machine disconnected")
        if connection_generation != self._connection_generation and self._latest is None:
            # Only a snapshot from the current connection can restore availability.
            raise UpdateFailed("Machine connection changed during refresh")
        if self._revision != revision and self._latest is not None:
            # A REST response started before a push must never rewind state.
            data = self._latest
        return data


class DecaidWaterCoordinator(DecaidPushCoordinator):
    """Water levels have a stream but no REST GET endpoint."""

    def __init__(self, hass, client):
        super().__init__(hass, client, "machine/waterLevels", STALE_SECONDS, "machine/waterLevels")
        self.last_update_success = False

    @callback
    def async_stream_lost(self):
        super().async_stream_lost()
        if not self._stopped:
            self.async_set_update_error(UpdateFailed("Water level stream unavailable"))

    async def _async_update_data(self):
        if (
            not self.machine_connected
            or not self.stream.active
            or self._latest is None
            or monotonic() - self.stream.received_at >= STALE_SECONDS
        ):
            raise UpdateFailed("Waiting for fresh water levels")
        return self._latest


@dataclass
class DecaidData:
    """Runtime data owned by a config entry."""

    client: DecaidClient
    machine: DecaidPushCoordinator
    workflow: DecaidCoordinator
    settings: DecaidCoordinator
    devices: DecaidPushCoordinator
    water: DecaidWaterCoordinator

    def machine_connected(self, machine_id: str | None) -> bool:
        return self.devices.last_update_success and any(
            device.get("type") == "machine"
            and (not machine_id or device.get("id") == machine_id)
            and device.get("state") == "connected"
            for device in (self.devices.data or [])
        )
