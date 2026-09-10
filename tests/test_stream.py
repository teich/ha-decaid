"""Push transport, resynchronization, and fallback regression tests."""

import asyncio
import json
from datetime import timedelta
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMessage, WSMsgType, web
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.decaid.api import DecaidClient, DecaidError
from custom_components.decaid.coordinator import (
    DecaidPushCoordinator,
    DecaidShotCoordinator,
    DecaidStreamCoordinator,
    DecaidWaterCoordinator,
)


def snapshot(state="idle", temp=90, substate="idle"):
    return {"state": {"state": state, "substate": substate}, "groupTemperature": temp}


def message(payload):
    return WSMessage(WSMsgType.TEXT, json.dumps(payload), "")


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.close = AsyncMock()
        self.ping = AsyncMock()
        self.pong = AsyncMock()

    async def receive(self):
        return await self.queue.get()


@pytest.fixture
async def coordinator(hass):
    client = MagicMock()
    client.get = AsyncMock(return_value=snapshot("sleeping"))
    result = DecaidPushCoordinator(hass, client, "machine/state", 10, "machine/snapshot")
    await result.async_refresh()
    unsub = result.async_add_listener(MagicMock())
    yield result
    unsub()
    await result.stop()
    await result.async_shutdown()


def push(coordinator, data):
    coordinator.stream.active = True
    coordinator.stream.received_at = monotonic()
    coordinator.async_receive(data)


async def test_push_throttle_and_immediate_states(hass, coordinator, freezer):
    push(coordinator, snapshot("heating", 80))
    assert coordinator.data["groupTemperature"] == 80
    push(coordinator, snapshot("heating", 81))
    push(coordinator, snapshot("heating", 82))
    assert coordinator.data["groupTemperature"] == 80
    freezer.tick(timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert coordinator.data["groupTemperature"] == 82
    push(coordinator, snapshot("espresso", 83, "preinfusion"))
    assert coordinator.data["state"]["substate"] == "preinfusion"
    push(coordinator, snapshot("espresso", 84, "pouring"))
    assert coordinator.data["state"]["substate"] == "pouring"
    await coordinator.async_refresh()
    coordinator.client.get.assert_awaited_once()  # Startup only.


@pytest.mark.parametrize("rest_fails", [False, True])
async def test_late_rest_cannot_overwrite_push(coordinator, rest_fails):
    started, finish = asyncio.Event(), asyncio.Event()

    async def rest(_path):
        started.set()
        await finish.wait()
        if rest_fails:
            raise DecaidError("late timeout")
        return snapshot("sleeping")

    coordinator.client.get.side_effect = rest
    refresh = asyncio.create_task(coordinator.async_refresh())
    await started.wait()
    push(coordinator, snapshot("espresso", 93))
    finish.set()
    await refresh
    assert coordinator.last_update_success
    assert coordinator.data["state"]["state"] == "espresso"


async def test_failure_falls_back_and_push_recovers(coordinator):
    push(coordinator, snapshot("idle", 90))
    coordinator.stream.active = False
    coordinator.client.get.return_value = snapshot("heating", 85)
    await coordinator.async_stream_failed()
    assert coordinator.data["state"]["state"] == "heating"
    assert coordinator.update_interval.total_seconds() == 1
    coordinator.client.get.side_effect = DecaidError("offline")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.update_interval.total_seconds() == 10
    push(coordinator, snapshot("idle", 93))
    assert coordinator.last_update_success
    assert coordinator.data["groupTemperature"] == 93


async def test_disconnect_blocks_telemetry_until_fresh_reconnect(coordinator):
    push(coordinator, snapshot("idle", 93))
    coordinator.set_device_connected(False)
    assert not coordinator.last_update_success
    push(coordinator, snapshot("idle", 94))
    assert not coordinator.last_update_success
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    coordinator.set_device_connected(True)
    assert not coordinator.last_update_success
    push(coordinator, snapshot("heating", 92))
    assert coordinator.last_update_success


async def test_stale_stream_uses_rest(coordinator):
    push(coordinator, snapshot("idle", 93))
    coordinator.stream.received_at -= 20
    coordinator.client.get.return_value = snapshot("sleeping", 50)
    await coordinator.async_refresh()
    assert coordinator.data["state"]["state"] == "sleeping"


async def test_socket_reconnects_and_normalizes_devices(hass):
    client = MagicMock()
    client.get = AsyncMock(return_value=[])
    c = DecaidPushCoordinator(hass, client, "devices", 60, "devices")
    sockets = [Socket(), Socket()]
    client.connect_stream = AsyncMock(side_effect=sockets)
    changes = asyncio.Queue()
    unsub = c.async_add_listener(lambda: changes.put_nowait(c.data))
    c.stream._task = asyncio.create_task(c.stream._run())
    try:
        await sockets[0].queue.put(
            message({"devices": [{"type": "machine", "state": "connected"}]})
        )
        assert (await asyncio.wait_for(changes.get(), 2))[0]["state"] == "connected"
        await sockets[0].queue.put(WSMessage(WSMsgType.CLOSED, None, None))
        assert await asyncio.wait_for(changes.get(), 2) == []  # REST fallback.
        await sockets[1].queue.put(
            message({"devices": [{"type": "machine", "state": "disconnected"}]})
        )
        assert (await asyncio.wait_for(changes.get(), 3))[0]["state"] == "disconnected"
        assert client.connect_stream.await_count == 2
        sockets[0].close.assert_awaited_once()
    finally:
        unsub()
        await c.stop()
        await c.async_shutdown()
    sockets[1].close.assert_awaited_once()


@pytest.mark.parametrize("payload", [{}, {"state": None}, {"state": {"state": 42}}])
async def test_bad_stream_frame_falls_back(coordinator, payload):
    socket = Socket()
    coordinator.client.connect_stream = AsyncMock(return_value=socket)
    fallback = asyncio.Event()
    original = coordinator.async_stream_failed

    async def failed():
        await original()
        fallback.set()

    with patch.object(coordinator, "async_stream_failed", side_effect=failed):
        coordinator.stream._task = asyncio.create_task(coordinator.stream._run())
        await socket.queue.put(message(payload))
        await asyncio.wait_for(fallback.wait(), 2)
        assert coordinator.data["state"]["state"] == "sleeping"
        assert not coordinator.stream.active
        await coordinator.stop()
    socket.close.assert_awaited_once()


async def test_stop_cancels_pending_numeric_publish(hass, coordinator, freezer):
    push(coordinator, snapshot("idle", 90))
    push(coordinator, snapshot("idle", 91))
    await coordinator.stop()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert coordinator.data["groupTemperature"] == 90


async def test_real_websocket_client(hass, aiohttp_server, socket_enabled):
    """Exercise aiohttp handshake, JSON messages, and close against a local server."""

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(snapshot("heating", 82))
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/ws/v1/machine/snapshot", handler)
    server = await aiohttp_server(app)
    client = DecaidClient(async_get_clientsession(hass), server.host, server.port)
    ws = await client.connect_stream("machine/snapshot")
    try:
        msg = await ws.receive()
        assert msg.json()["state"]["state"] == "heating"
    finally:
        await ws.close()


@pytest.mark.parametrize("connected", [False, True])
async def test_silent_machine_only_reconnects_when_expected(coordinator, connected):
    socket = Socket()
    checked = asyncio.Event()
    fallback = asyncio.Event()
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError
        checked.set()
        return await socket.queue.get()

    socket.receive = receive
    coordinator.set_device_connected(connected)
    coordinator.client.connect_stream = AsyncMock(return_value=socket)
    coordinator.async_stream_failed = AsyncMock(side_effect=lambda: fallback.set())
    with (
        patch("custom_components.decaid.stream.STALE_SECONDS", 0),
        patch("custom_components.decaid.stream.RECEIVE_CHECK_SECONDS", 1),
    ):
        coordinator.stream._task = asyncio.create_task(coordinator.stream._run())
        if connected:
            await asyncio.wait_for(fallback.wait(), 2)
            socket.close.assert_awaited_once()
        else:
            await asyncio.wait_for(checked.wait(), 2)
            socket.close.assert_not_awaited()
            coordinator.async_stream_failed.assert_not_awaited()
        await coordinator.stop()


async def test_reconnect_backoff_and_cancellation(coordinator):
    delays = []
    enough = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 8:
            enough.set()
            await asyncio.Event().wait()
        await real_sleep(0)

    coordinator.client.connect_stream = AsyncMock(side_effect=OSError("network down"))
    with (
        patch("custom_components.decaid.stream.asyncio.sleep", side_effect=sleep),
        patch("custom_components.decaid.stream.random.uniform", side_effect=lambda low, high: high),
    ):
        coordinator.stream._task = asyncio.create_task(coordinator.stream._run())
        await asyncio.wait_for(enough.wait(), 2)
        assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
        await coordinator.stop()
        assert coordinator.stream._task is None


async def test_disconnect_during_rest_does_not_restore_old_state(coordinator):
    started, finish = asyncio.Event(), asyncio.Event()

    async def rest(_path):
        started.set()
        await finish.wait()
        return snapshot("idle")

    coordinator.client.get.side_effect = rest
    task = asyncio.create_task(coordinator.async_refresh())
    await started.wait()
    coordinator.set_device_connected(False)
    finish.set()
    await task
    assert not coordinator.last_update_success


@pytest.mark.parametrize("reply", [True, False])
async def test_heartbeat_pong_and_timeout(coordinator, reply):
    socket = Socket()
    now = 0
    count = 0
    waiting, fallback = asyncio.Event(), asyncio.Event()

    async def receive():
        nonlocal now, count
        count += 1
        if count == 1:
            now = 21
            return message(snapshot("idle"))
        if count == 2:
            now = 22 if reply else 32
            return WSMessage(WSMsgType.PONG, b"decaid" if reply else b"other", "")
        if count == 3:
            now = 25
            return WSMessage(WSMsgType.PING, b"server-ping", "")
        waiting.set()
        return await socket.queue.get()

    socket.receive = receive
    coordinator.client.connect_stream = AsyncMock(return_value=socket)
    coordinator.async_stream_failed = AsyncMock(side_effect=lambda: fallback.set())
    with patch("custom_components.decaid.stream.monotonic", side_effect=lambda: now):
        coordinator.stream._task = asyncio.create_task(coordinator.stream._run())
        await asyncio.wait_for((waiting if reply else fallback).wait(), 2)
        socket.ping.assert_awaited_once_with(b"decaid")
        if reply:
            socket.pong.assert_awaited_once_with(b"server-ping")
            socket.close.assert_not_awaited()
            coordinator.async_stream_failed.assert_not_awaited()
        else:
            socket.close.assert_awaited_once()
        await coordinator.stop()


async def test_late_rest_cannot_rewind_frame_even_after_socket_closes(coordinator):
    started, finish = asyncio.Event(), asyncio.Event()

    async def rest(_path):
        started.set()
        await finish.wait()
        return snapshot("sleeping")

    coordinator.client.get.side_effect = rest
    task = asyncio.create_task(coordinator.async_refresh())
    await started.wait()
    push(coordinator, snapshot("espresso", 93))
    coordinator.stream.active = False
    coordinator.async_stream_lost()
    finish.set()
    await task
    assert coordinator.data["state"]["state"] == "espresso"


@pytest.mark.parametrize("rest_fails", [False, True])
@pytest.mark.parametrize("fresh_push", [False, True])
async def test_rest_crossing_reconnect_requires_fresh_data(coordinator, rest_fails, fresh_push):
    started, finish = asyncio.Event(), asyncio.Event()

    async def rest(_path):
        started.set()
        await finish.wait()
        if rest_fails:
            raise DecaidError("old request failed")
        return snapshot("idle", 80)

    coordinator.client.get.side_effect = rest
    task = asyncio.create_task(coordinator.async_refresh())
    await started.wait()
    coordinator.set_device_connected(False)
    coordinator.set_device_connected(True)
    if fresh_push:
        push(coordinator, snapshot("espresso", 93))
    finish.set()
    await task
    assert coordinator.last_update_success is fresh_push
    if fresh_push:
        assert coordinator.data == snapshot("espresso", 93)
    else:
        # A request begun after reconnect can recover normally.
        coordinator.client.get.side_effect = None
        coordinator.client.get.return_value = snapshot("heating", 85)
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert coordinator.data == snapshot("heating", 85)


@pytest.mark.parametrize("frame_type", [WSMsgType.PING, WSMsgType.PONG])
@pytest.mark.parametrize(
    ("path", "connected", "initial_snapshot", "should_fail"),
    [
        ("machine/state", True, True, True),
        ("machine/state", False, False, False),
        ("machine/waterLevels", True, True, True),
        ("machine/waterLevels", False, False, False),
        ("scale/snapshot", True, True, True),
        ("scale/snapshot", False, False, False),
        ("machine/shotState", True, False, True),
        ("machine/shotState", True, True, False),
        ("devices", True, False, True),
        ("devices", True, True, False),
    ],
)
async def test_control_frames_do_not_refresh_snapshots(
    hass, frame_type, path, connected, initial_snapshot, should_fail, shot_payload
):
    socket = Socket()
    client = MagicMock(connect_stream=AsyncMock(return_value=socket))
    c = DecaidPushCoordinator(
        hass, client, path, 10, "machine/snapshot" if path == "machine/state" else path
    )
    c.set_device_connected(connected)
    now = 0
    sent_initial = False
    checked, fallback = asyncio.Event(), asyncio.Event()

    async def receive():
        nonlocal now, sent_initial
        if initial_snapshot and not sent_initial:
            sent_initial = True
            if path == "machine/waterLevels":
                return message({"currentLevel": 28.3, "refillLevel": 5})
            if path == "scale/snapshot":
                return message({"weight": 0, "weightFlow": 0})
            if path == "machine/shotState":
                return message(shot_payload)
            return message({"devices": []} if path == "devices" else snapshot())
        if now >= 40:
            checked.set()
            return await socket.queue.get()
        now += 1
        # No receive timeout, even though application data has stopped.
        await asyncio.sleep(0)
        return WSMessage(frame_type, b"decaid", "")

    socket.receive = receive
    c.async_stream_failed = AsyncMock(side_effect=fallback.set)
    with (
        patch("custom_components.decaid.stream.monotonic", side_effect=lambda: now),
        # Keep transport healthy for both PING-only and PONG-only cases.
        patch("custom_components.decaid.stream.PING_INTERVAL", 100),
    ):
        c.stream._task = asyncio.create_task(c.stream._run())
        try:
            await asyncio.wait_for((fallback if should_fail else checked).wait(), 2)
            if should_fail:
                assert now == 15
                socket.close.assert_awaited_once()
            else:
                socket.close.assert_not_awaited()
                c.async_stream_failed.assert_not_awaited()
        finally:
            await c.stop()
            await c.async_shutdown()


async def test_unchanged_pushes_keep_watchdog_fresh_without_notifications(
    hass, coordinator, freezer
):
    listener = MagicMock()
    unsub = coordinator.async_add_listener(listener)
    try:
        push(coordinator, snapshot())
        listener.assert_called_once()
        listener.reset_mock()
        for _ in range(20):
            freezer.tick(timedelta(seconds=1))
            push(coordinator, snapshot())
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        listener.assert_not_called()
        coordinator.client.get.assert_awaited_once()  # Startup only.
        coordinator.set_device_connected(False)
        coordinator.set_device_connected(True)
        listener.reset_mock()
        push(coordinator, snapshot())
        listener.assert_called_once()  # Equal values still restore availability.
        assert coordinator.last_update_success
        coordinator.stream.active = False
        coordinator.client.get.return_value = snapshot("sleeping")
        freezer.tick(timedelta(seconds=16))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert coordinator.data == snapshot("sleeping")
        assert coordinator.client.get.await_count == 2
    finally:
        unsub()


@pytest.mark.parametrize("failure", ["closed", "stale", "invalid"])
async def test_water_stream_failure_and_recovery_without_rest(hass, failure):
    client = MagicMock(get=AsyncMock())
    c = DecaidWaterCoordinator(hass, client)
    socket = Socket()
    client.connect_stream = AsyncMock(return_value=socket)
    changes = asyncio.Queue()
    unsub = c.async_add_listener(lambda: changes.put_nowait(c.last_update_success))
    c.stream._task = asyncio.create_task(c.stream._run())
    try:
        assert not c.last_update_success
        await socket.queue.put(message({"currentLevel": 28.3, "refillLevel": 5}))
        assert await asyncio.wait_for(changes.get(), 2)
        assert c.data == {"currentLevel": 28.3, "refillLevel": 5}
        if failure == "stale":
            c.stream.received_at -= 16
            await c.async_refresh()
        else:
            await socket.queue.put(
                message({"currentLevel": None, "refillLevel": 5})
                if failure == "invalid"
                else WSMessage(WSMsgType.CLOSED, None, None)
            )
        assert not await asyncio.wait_for(changes.get(), 2)
        # Force immediate reconnect for the stale case as well.
        if failure == "stale":
            await socket.queue.put(WSMessage(WSMsgType.CLOSED, None, None))
        await socket.queue.put(message({"currentLevel": 29, "refillLevel": 5}))
        assert await asyncio.wait_for(changes.get(), 3)
        assert c.data == {"currentLevel": 29, "refillLevel": 5}
        client.get.assert_not_awaited()
    finally:
        unsub()
        await c.stop()
        await c.async_shutdown()


async def test_scale_status_disconnect_and_reconnect_on_same_socket(hass):
    client = MagicMock(get=AsyncMock())
    c = DecaidStreamCoordinator(hass, client, "scale/snapshot")
    socket = Socket()
    client.connect_stream = AsyncMock(return_value=socket)
    changes = asyncio.Queue()
    unsub = c.async_add_listener(lambda: changes.put_nowait(c.last_update_success))
    c.stream._task = asyncio.create_task(c.stream._run())
    try:
        await socket.queue.put(message({"status": "connected"}))
        await socket.queue.put(message({"weight": 36, "weightFlow": 2}))
        assert await asyncio.wait_for(changes.get(), 2)
        # A queued numerical publication must not survive scale disconnect.
        await socket.queue.put(message({"weight": 37, "weightFlow": 2}))
        await socket.queue.put(message({"status": "disconnected"}))
        assert not await asyncio.wait_for(changes.get(), 2)
        assert c._cancel_publish is None
        assert not c.device_connected
        socket.close.assert_not_awaited()
        await socket.queue.put(message({"status": "connected"}))
        # Synchronize with the status handler before sending fresh telemetry.
        await asyncio.sleep(0)
        await c.async_refresh()
        assert not c.last_update_success
        await socket.queue.put(message({"weight": 1, "weightFlow": 0}))
        assert await asyncio.wait_for(changes.get(), 2)
        assert c.data["weight"] == 1
        client.get.assert_not_awaited()
        client.connect_stream.assert_awaited_once()
    finally:
        unsub()
        await c.stop()
        await c.async_shutdown()


async def test_shot_socket_replay_and_quiet_watchdog(hass, shot_payload):
    client = MagicMock(get=AsyncMock())
    c = DecaidShotCoordinator(hass, client)
    socket = Socket()
    client.connect_stream = AsyncMock(return_value=socket)
    changes = asyncio.Queue()
    unsub = c.async_add_listener(lambda: changes.put_nowait((c.last_update_success, c.is_replay)))
    c.stream._task = asyncio.create_task(c.stream._run())
    try:
        await socket.queue.put(message(shot_payload))
        assert await asyncio.wait_for(changes.get(), 2) == (True, True)
        c.stream.received_at -= 300  # Quiet idle is valid after the initial frame.
        await c.async_refresh()
        assert c.last_update_success
        assert changes.empty()
        terminal = {
            **shot_payload,
            "event": "terminal",
            "state": "finished",
            "decision": {"kind": "terminal", "reason": "disconnected"},
        }
        await socket.queue.put(message(terminal))
        assert await asyncio.wait_for(changes.get(), 2) == (True, False)
        await socket.queue.put(WSMessage(WSMsgType.CLOSED, None, None))
        assert await asyncio.wait_for(changes.get(), 2) == (False, False)
        await socket.queue.put(message(terminal))
        assert await asyncio.wait_for(changes.get(), 3) == (True, True)
        assert c.data["stopReason"] == "disconnected"
        client.get.assert_not_awaited()
    finally:
        unsub()
        await c.stop()
        await c.async_shutdown()
