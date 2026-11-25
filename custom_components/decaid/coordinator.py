"""Push telemetry with independent REST polling and fallback."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DecaidClient, DecaidError
from .stream import STALE_SECONDS, DecaidStream

_LOGGER = logging.getLogger(__name__)

# Numerical pushes publish at most this often. A sleeping machine still streams
# temperatures; publishing those every second would flood the recorder.
PUBLISH_SECONDS = 1
SLEEPING_PUBLISH_SECONDS = STALE_SECONDS


def _parse_timestamp(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class DecaidCoordinator(DataUpdateCoordinator):
    """Fetch one endpoint without coupling its availability to the others."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DecaidClient,
        path: str,
        seconds: int,
        *,
        config_entry: ConfigEntry | None = None,
    ):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
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

    def __init__(self, hass, client, path, seconds, stream_path, **kwargs):
        super().__init__(hass, client, path, seconds, **kwargs)
        self.stream = DecaidStream(self, stream_path)
        self.device_connected = True
        self._latest = None
        self._revision = 0
        self._connection_generation = 0
        self._published_at = 0.0
        self._cancel_publish = None
        self._stopped = False

    @property
    def event_driven(self):
        return self.path in (
            "devices",
            "machine/shotState",
            "machine/shotSettings",
            "machine/waterLevels",
            "update",
        )

    @property
    def expects_stream_data(self):
        return not self.stream.active if self.event_driven else self.device_connected

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
    def set_device_connected(self, connected):
        if connected == self.device_connected:
            return
        self.device_connected = connected
        self._connection_generation += 1
        self._latest = None
        self._revision += 1
        self._cancel_pending_publish()
        if not connected:
            self.async_set_update_error(UpdateFailed("Device disconnected"))

    @callback
    def async_receive(self, data, *, initial=False):
        """Remember every frame, publish state changes immediately and numbers at 1 Hz."""
        if self._stopped or not self.device_connected:
            return
        self._latest = data
        self._revision += 1
        state_changed = self.path == "machine/state" and (
            not self.data or data["state"] != self.data.get("state")
        )
        interval = (
            SLEEPING_PUBLISH_SECONDS
            if self.path == "machine/state" and data["state"].get("state") == "sleeping"
            else PUBLISH_SECONDS
        )
        elapsed = monotonic() - self._published_at
        if (
            self.event_driven
            or state_changed
            or not self.last_update_success
            or elapsed >= interval
        ):
            self._publish()
        elif self._cancel_publish is None:
            self._cancel_publish = async_call_later(self.hass, interval - elapsed, self._publish)

    @callback
    def _publish(self, _now=None):
        self._cancel_pending_publish()
        if self._stopped or self._latest is None or not self.device_connected:
            return
        self._published_at = monotonic()
        # Changed pushes postpone this watchdog. Unchanged fresh frames satisfy
        # it from the cache; a silent machine falls back to REST.
        self.update_interval = timedelta(seconds=60 if self.event_driven else STALE_SECONDS)
        if self.last_update_success and self._latest == self.data:
            return
        self.async_set_updated_data(self._latest)

    @callback
    def async_stream_lost(self):
        """Cancel queued publications before closing a broken socket."""
        # Retain the last frame for revision comparisons with in-flight REST.
        # stream.active is false, so it cannot satisfy a new refresh request.
        self._cancel_pending_publish()
        # Device inventory drives machine availability, so poll it as often as
        # machine state while its stream is down.
        self.update_interval = timedelta(
            seconds=10 if self.path in ("machine/state", "devices") else 60
        )

    async def async_stream_failed(self):
        self.async_stream_lost()
        if not self._stopped:
            await self.async_refresh()

    async def _async_update_data(self):
        if not self.device_connected:
            self.update_interval = timedelta(seconds=10)
            raise UpdateFailed("Machine disconnected")
        if (
            self.stream.active
            and self._latest is not None
            # Event-driven streams stay quiet while nothing changes; an open
            # socket keeps their last frame current.
            and (self.event_driven or monotonic() - self.stream.received_at <= 2)
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
        if not self.device_connected:
            raise UpdateFailed("Machine disconnected")
        if connection_generation != self._connection_generation and self._latest is None:
            # Only a snapshot from the current connection can restore availability.
            raise UpdateFailed("Machine connection changed during refresh")
        if self._revision != revision and self._latest is not None:
            # A REST response started before a push must never rewind state.
            data = self._latest
        return data


class DecaidStreamCoordinator(DecaidPushCoordinator):
    """Optional telemetry without a REST fallback."""

    def __init__(self, hass, client, path, **kwargs):
        super().__init__(hass, client, path, STALE_SECONDS, path, **kwargs)
        self.last_update_success = False

    @callback
    def async_stream_lost(self):
        super().async_stream_lost()
        if not self._stopped:
            self.async_set_update_error(UpdateFailed(f"{self.path} stream unavailable"))

    async def _async_update_data(self):
        if (
            not self.device_connected
            or not self.stream.active
            or self._latest is None
            or (not self.event_driven and monotonic() - self.stream.received_at >= STALE_SECONDS)
        ):
            raise UpdateFailed(f"Waiting for fresh {self.path} data")
        return self._latest


class DecaidScaleCoordinator(DecaidStreamCoordinator):
    """Scale telemetry; a fresh measurement proves the scale is connected."""

    def __init__(self, hass, client, **kwargs):
        super().__init__(hass, client, "scale/snapshot", **kwargs)

    @callback
    def async_receive(self, data, *, initial=False):
        # Current Decaid resends the scale status on every new socket, but a
        # build that only reports transitions could otherwise leave readings
        # blocked after a reconnect.
        if not self._stopped and not self.device_connected:
            self.set_device_connected(True)
        super().async_receive(data, initial=initial)


class DecaidWaterCoordinator(DecaidPushCoordinator):
    """Water levels stream on change, with REST while the stream is down.

    Decaid builds before November 2026 have no REST endpoint; there the
    levels are unavailable whenever the stream is.
    """

    def __init__(self, hass, client, **kwargs):
        super().__init__(hass, client, "machine/waterLevels", 60, "machine/waterLevels", **kwargs)

    @property
    def expects_stream_data(self):
        return self.device_connected and (not self.stream.active or self._latest is None)


class DecaidShotSettingsCoordinator(DecaidStreamCoordinator):
    """Settings may remain unchanged, but require a fresh snapshot after reconnect."""

    def __init__(self, hass, client, **kwargs):
        super().__init__(hass, client, "machine/shotSettings", **kwargs)

    @property
    def expects_stream_data(self):
        return self.device_connected and (not self.stream.active or self._latest is None)


class DecaidShotCoordinator(DecaidStreamCoordinator):
    """Publish every sequencer event; distinguish the initial replay from live events."""

    def __init__(self, hass, client, **kwargs):
        super().__init__(hass, client, "machine/shotState", **kwargs)
        self.is_replay = True
        self._stop_reason = None
        self._newest_timestamp = None

    @callback
    def restore_stop_reason(self, reason):
        """Seed the last stop reason from before a restart; live frames win."""
        if self._stop_reason is None:
            self._stop_reason = reason

    def _is_replay(self, data, initial):
        """Decaid replays its latest frame on connect.

        That frame is only new if it is newer than every frame seen so far;
        then it is an event that happened while the socket was down. The very
        first frame after startup is always treated as a replay.
        """
        timestamp = _parse_timestamp(data["timestamp"])
        newest = self._newest_timestamp
        try:
            if timestamp is not None and (newest is None or timestamp > newest):
                self._newest_timestamp = timestamp
            return initial and (newest is None or timestamp is None or timestamp <= newest)
        except TypeError:  # Naive and aware timestamps cannot be compared.
            self._newest_timestamp = timestamp
            return initial

    @callback
    def async_receive(self, data, *, initial=False):
        if self._stopped:
            return
        self.is_replay = self._is_replay(data, initial)
        decision = data.get("decision") or {}
        if decision.get("kind") in ("stop", "abort", "terminal"):
            self._stop_reason = decision.get("reason")
        self._latest = {**data, "stopReason": self._stop_reason}
        self._revision += 1
        self.update_interval = timedelta(seconds=60)
        # No coalescing or equality suppression: two successive decisions must
        # remain two events even when their state or timestamp is identical.
        self.async_set_updated_data(self._latest)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def summarize_shot(shot):
    """Reduce a full shot record's measurements to a few headline figures."""
    summary = {}
    measurements = shot.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        return summary
    machines = [m.get("machine") for m in measurements if isinstance(m, dict)]
    machines = [m for m in machines if isinstance(m, dict)]
    times = [_parse_timestamp(m.get("timestamp")) for m in machines]
    times = [t for t in times if t is not None]
    if len(times) >= 2:
        try:
            summary["duration"] = round((times[-1] - times[0]).total_seconds(), 1)
        except TypeError:
            pass
    for key, field in (("peak_pressure", "pressure"), ("peak_flow", "flow")):
        values = [v for v in (_number(m.get(field)) for m in machines) if v is not None]
        if values:
            summary[key] = round(max(values), 1)
    for measurement in reversed(measurements):
        scale = measurement.get("scale") if isinstance(measurement, dict) else None
        weight = _number(scale.get("weight")) if isinstance(scale, dict) else None
        if weight is not None:
            summary["final_weight"] = round(weight, 1)
            break
    return summary


class DecaidLastShotCoordinator(DecaidCoordinator):
    """The newest shot, with its measurements summarized."""

    def __init__(self, hass, client, **kwargs):
        super().__init__(hass, client, "shots?limit=1", 60, **kwargs)
        self._summary_id = None
        self._summary = {}

    async def _async_update_data(self):
        data = await super()._async_update_data()
        items = data.get("items")
        latest = items[0] if isinstance(items, list) and items else None
        if not isinstance(latest, dict) or not isinstance(latest.get("id"), str):
            return {**data, "latest": None}
        if latest["id"] != self._summary_id:
            try:
                shot = await self.client.get(f"shots/{latest['id']}")
            except DecaidError as err:
                # The listing is still useful; retry the summary next poll.
                _LOGGER.debug("Unable to read Decaid shot %s: %s", latest["id"], err)
                return {**data, "latest": latest, "summary": {}}
            self._summary_id = latest["id"]
            self._summary = summarize_shot(shot)
        return {**data, "latest": latest, "summary": self._summary}


@dataclass
class DecaidData:
    """Runtime data owned by a config entry."""

    client: DecaidClient
    machine: DecaidPushCoordinator
    workflow: DecaidCoordinator
    settings: DecaidCoordinator
    devices: DecaidPushCoordinator
    water: DecaidWaterCoordinator
    scale: DecaidScaleCoordinator
    shot: DecaidShotCoordinator
    shot_settings: DecaidShotSettingsCoordinator
    shots: DecaidLastShotCoordinator
    machine_info: DecaidCoordinator
    capabilities: DecaidCoordinator
    machine_settings: DecaidCoordinator
    display: DecaidCoordinator
    presence: DecaidCoordinator
    app_update: DecaidPushCoordinator
    firmware: DecaidCoordinator
    cup_warmer: DecaidCoordinator
    preheat: DecaidCoordinator

    def has_capability(self, capability: str) -> bool:
        caps = (self.capabilities.data or {}).get("capabilities")
        return isinstance(caps, list) and capability in caps

    @property
    def streams(self):
        return (
            self.machine,
            self.devices,
            self.water,
            self.scale,
            self.shot,
            self.shot_settings,
            self.app_update,
        )

    def machine_connected(self, machine_id: str | None) -> bool:
        return self.devices.last_update_success and any(
            device.get("type") == "machine"
            and (not machine_id or device.get("id") == machine_id)
            and device.get("state") == "connected"
            for device in (self.devices.data or [])
        )
