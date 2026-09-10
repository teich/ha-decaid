"""Persistent Decaid streams with heartbeat, reconnect, and bounded backoff."""

import asyncio
import logging
import random
from time import monotonic

from aiohttp import ClientError, WSMsgType

from .api import DecaidError, validate_payload

_LOGGER = logging.getLogger(__name__)
STALE_SECONDS = 15
RECEIVE_CHECK_SECONDS = 5
PING_INTERVAL = 20
PONG_TIMEOUT = 10


class DecaidStream:
    """Own a single socket; a quiet disconnected machine is not a dead tablet."""

    def __init__(self, coordinator, path):
        self.coordinator = coordinator
        self.path = path
        self.active = False
        self.received_at = 0.0
        self._stopped = False
        self._task = None

    def start(self, entry):
        self._task = entry.async_create_background_task(
            self.coordinator.hass, self._run(), f"Decaid {self.path} stream"
        )

    async def stop(self):
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.active = False

    async def _run(self):
        delay = 1
        while not self._stopped:
            socket = None
            opened_at = monotonic()
            try:
                socket = await self.coordinator.client.connect_stream(self.path)
                opened_at = monotonic()
                self.received_at = opened_at
                next_ping = opened_at + PING_INTERVAL
                pending_ping = None
                while not self._stopped:
                    now = monotonic()
                    # Control traffic proves transport health, not snapshot
                    # freshness. Check this even when receive() never times out.
                    expects_data = (
                        self.coordinator.machine_connected
                        if self.path in ("machine/snapshot", "machine/waterLevels")
                        else not self.active
                    )
                    if expects_data and now - self.received_at >= STALE_SECONDS:
                        raise DecaidError("Stream stopped delivering snapshots")
                    if pending_ping is not None and now - pending_ping >= PONG_TIMEOUT:
                        raise DecaidError("WebSocket heartbeat timed out")
                    if pending_ping is None and now >= next_ping:
                        async with asyncio.timeout(5):
                            await socket.ping(b"decaid")
                        pending_ping = now
                        next_ping = now + PING_INTERVAL
                    try:
                        async with asyncio.timeout(RECEIVE_CHECK_SECONDS):
                            message = await socket.receive()
                    except TimeoutError:
                        continue
                    if message.type == WSMsgType.PING:
                        async with asyncio.timeout(5):
                            await socket.pong(message.data)
                        continue
                    if message.type == WSMsgType.PONG:
                        if message.data == b"decaid":
                            pending_ping = None
                        continue
                    if message.type != WSMsgType.TEXT:
                        raise DecaidError(f"Stream closed or failed: {message.type.name}")
                    payload = message.json()
                    if self.path == "devices":
                        if not isinstance(payload, dict) or "devices" not in payload:
                            raise DecaidError("Invalid devices stream snapshot")
                        payload = payload["devices"]
                    data = validate_payload(self.coordinator.path, payload)
                    self.received_at = monotonic()
                    self.active = True
                    self.coordinator.async_receive(data)
            except (ClientError, OSError, TimeoutError, ValueError, DecaidError) as err:
                _LOGGER.debug("Decaid %s stream interrupted: %s", self.path, err)
            finally:
                self.active = False
                self.coordinator.async_stream_lost()
                if socket is not None:
                    await socket.close()
            if not self._stopped:
                await self.coordinator.async_stream_failed()
                # A socket that repeatedly opens then immediately fails must
                # back off too. Reset only after a sustained healthy session.
                if monotonic() - opened_at >= 60:
                    delay = 1
                await asyncio.sleep(random.uniform(delay * 0.8, delay))
                delay = min(delay * 2, 60)
