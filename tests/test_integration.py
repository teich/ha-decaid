"""Exercise config flows and real HA entity/platform lifecycle."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.decaid.api import DecaidError


@pytest.fixture
def payloads():
    return {
        "machine/state": {
            "state": {"state": "sleeping", "substate": "idle"},
            "groupTemperature": 92.4,
            "flow": 0,
        },
        "workflow": {
            "profile": {"title": "Test profile"},
            "context": {"targetDoseWeight": 18, "targetYield": 36},
        },
        "settings": {"chargingState": {"batteryPercent": 80}},
        "devices": [
            {"type": "machine", "id": "abc", "state": "connected"},
            {"type": "scale", "state": "connected"},
        ],
    }


@pytest.fixture
async def setup_entry(hass, payloads):
    async def get(path):
        result = payloads[path]
        if isinstance(result, Exception):
            raise result
        return result

    entry = MockConfigEntry(
        domain="decaid", data={"host": "192.168.2.231", "port": 8080, "machine_id": ""}
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.decaid.api.DecaidClient.get", side_effect=get),
        patch(
            "custom_components.decaid.api.DecaidClient.set_power", new_callable=AsyncMock
        ) as power,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, power
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_entities(hass, setup_entry):
    entry, _ = setup_entry
    assert len(hass.states.async_all()) == 28
    assert hass.states.get("sensor.decent_water_level").state == "unavailable"
    assert hass.states.get("sensor.decent_refill_threshold").state == "unavailable"
    assert hass.states.get("sensor.decent_grouphead_temperature").state == "92.4"
    assert hass.states.get("sensor.decent_target_dose").state == "18.0"
    assert hass.states.get("binary_sensor.decent_machine_connected").state == "on"
    assert hass.states.get("switch.decent_power").state == "off"
    assert entry.runtime_data.machine.update_interval.total_seconds() == 10
    assert entry.runtime_data.settings.update_interval.total_seconds() == 60


async def test_only_changed_entities_write_state(hass, setup_entry, payloads, freezer):
    from datetime import timedelta

    entry, _ = setup_entry
    temperature_id = "sensor.decent_grouphead_temperature"
    state_id = "sensor.decent_state"
    connection_id = "binary_sensor.decent_machine_connected"
    temperature = hass.states.get(temperature_id)
    state = hass.states.get(state_id)
    connection = hass.states.get(connection_id)
    freezer.tick(timedelta(seconds=1))
    # Timestamp and unrelated device metadata do not change entity values.
    payloads["machine/state"] = {**payloads["machine/state"], "timestamp": "new"}
    payloads["devices"] = [{**device, "name": "new"} for device in payloads["devices"]]
    await entry.runtime_data.machine.async_refresh()
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get(temperature_id).last_reported == temperature.last_reported
    assert hass.states.get(state_id).last_reported == state.last_reported
    assert hass.states.get(connection_id).last_reported == connection.last_reported

    payloads["machine/state"] = {**payloads["machine/state"], "groupTemperature": 95}
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get(temperature_id).state == "95.0"
    assert hass.states.get(temperature_id).last_reported > temperature.last_reported
    assert hass.states.get(state_id).last_reported == state.last_reported

    good = payloads["machine/state"]
    payloads["machine/state"] = DecaidError("offline")
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get(temperature_id).state == "unavailable"
    payloads["machine/state"] = good
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get(temperature_id).state == "95.0"


async def test_power_guard_and_errors(hass, setup_entry, payloads):
    entry, power = setup_entry

    async def call(service):
        await hass.services.async_call(
            "switch", service, {"entity_id": "switch.decent_power"}, blocking=True
        )

    await call("turn_on")
    power.assert_awaited_once_with(True)
    power.reset_mock()
    payloads["machine/state"] = {"state": {"state": "espresso"}}
    await call("turn_on")
    power.assert_not_awaited()
    await call("turn_off")
    power.assert_awaited_once_with(False)
    power.side_effect = DecaidError("HTTP 503")
    with pytest.raises(HomeAssistantError):
        await call("turn_off")
    power.reset_mock()
    payloads["devices"] = []
    with pytest.raises(HomeAssistantError):
        await call("turn_off")
    power.assert_not_awaited()


async def test_independent_failures_and_recovery(hass, setup_entry, payloads):
    entry, _ = setup_entry
    payloads["settings"] = DecaidError("offline")
    await entry.runtime_data.settings.async_refresh()
    assert hass.states.get("sensor.decent_tablet_battery").state == "unavailable"
    assert hass.states.get("sensor.decent_grouphead_temperature").state == "92.4"
    payloads["settings"] = {"chargingState": {"batteryPercent": "NaN"}}
    await entry.runtime_data.settings.async_refresh()
    assert hass.states.get("sensor.decent_tablet_battery").state == "unknown"
    payloads["settings"] = {"chargingState": {"batteryPercent": 75}}
    await entry.runtime_data.settings.async_refresh()
    assert hass.states.get("sensor.decent_tablet_battery").state == "75.0"
    payloads["devices"] = DecaidError("offline")
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get("switch.decent_power").state == "unavailable"


async def test_flow(hass):
    result = await hass.config_entries.flow.async_init("decaid", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "http://bad", "port": 8080}
    )
    assert result["errors"] == {"host": "invalid_host"}
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=DecaidError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.2.231", "port": 8080}
        )
    assert result["errors"] == {"base": "cannot_connect"}
    with (
        patch(
            "custom_components.decaid.api.DecaidClient.get",
            return_value={"state": {"state": "idle"}},
        ),
        patch("custom_components.decaid.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.2.231", "port": 8080}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
    duplicate = await hass.config_entries.flow.async_init(
        "decaid", context={"source": SOURCE_USER}, data={"host": "192.168.2.231", "port": 8080}
    )
    assert duplicate["reason"] == "already_configured"


async def test_reconfigure(hass, setup_entry):
    entry, _ = setup_entry
    original_id = hass.states.get("switch.decent_power").entity_id
    result = await hass.config_entries.flow.async_init(
        "decaid", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.232", "port": 8080}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["host"] == "192.168.2.232"
    assert hass.states.get(original_id) is not None


async def test_machine_id_filter(hass, setup_entry, payloads):
    entry, _ = setup_entry
    data = entry.runtime_data
    assert data.machine_connected("abc")
    assert not data.machine_connected("different")
    payloads["devices"] = [{"type": "machine", "id": "abc", "state": "disconnected"}]
    await data.devices.async_refresh()
    assert not data.machine_connected("abc")
    assert hass.states.get("binary_sensor.decent_machine_connected").state == "off"
    assert hass.states.get("switch.decent_power").state == "unavailable"


async def test_setup_retry_and_optional_failure(hass):
    from homeassistant.config_entries import ConfigEntryState

    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=DecaidError("offline")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    async def get(path):
        if path == "machine/state":
            return {"state": {"state": "sleeping"}}
        raise DecaidError("optional endpoint unavailable")

    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=get):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.decent_state").state == "sleeping"
        assert hass.states.get("sensor.decent_profile").state == "unavailable"
        assert hass.states.get("switch.decent_power").state == "unavailable"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("initial", "service", "expected", "confirmed"),
    [("sleeping", "turn_on", "on", "heating"), ("idle", "turn_off", "off", "sleeping")],
)
async def test_power_transition_waits_for_confirmation(
    hass, setup_entry, payloads, initial, service, expected, confirmed
):
    entry, _ = setup_entry
    payloads["machine/state"] = {"state": {"state": initial}}
    await entry.runtime_data.machine.async_refresh()
    await hass.services.async_call(
        "switch", service, {"entity_id": "switch.decent_power"}, blocking=True
    )
    # The immediate read still reports the old state, but the switch stays put.
    assert hass.states.get("switch.decent_power").state == expected
    assert hass.states.get("sensor.decent_state").state == initial
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get("switch.decent_power").state == expected
    payloads["machine/state"] = {"state": {"state": confirmed}}
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get("switch.decent_power").state == expected
    # Once confirmed, subsequent external changes must appear immediately.
    payloads["machine/state"] = {"state": {"state": initial}}
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get("switch.decent_power").state != expected


async def test_power_transition_expires(hass, setup_entry, freezer):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.decent_power"}, blocking=True
    )
    assert hass.states.get("switch.decent_power").state == "on"
    # Unchanged coordinator data suppresses notifications; the timer must still expire.
    freezer.tick(timedelta(seconds=21))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("switch.decent_power").state == "off"


async def test_failed_power_command_is_not_optimistic(hass, setup_entry):
    _, power = setup_entry
    power.side_effect = DecaidError("HTTP 503")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.decent_power"}, blocking=True
        )
    assert hass.states.get("switch.decent_power").state == "off"


async def test_pending_power_connection_loss(hass, setup_entry, payloads):
    entry, _ = setup_entry
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.decent_power"}, blocking=True
    )
    assert hass.states.get("switch.decent_power").state == "on"
    payloads["devices"] = []
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get("switch.decent_power").state == "unavailable"
    payloads["devices"] = [{"type": "machine", "state": "connected"}]
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get("switch.decent_power").state == "unavailable"
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get("switch.decent_power").state == "off"


async def test_wake_during_pending_sleep_uses_reported_state(hass, setup_entry, payloads):
    entry, power = setup_entry
    payloads["machine/state"] = {"state": {"state": "idle"}}
    await entry.runtime_data.machine.async_refresh()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.decent_power"}, blocking=True
    )
    assert hass.states.get("switch.decent_power").state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.decent_power"}, blocking=True
    )
    # An optimistic sleep display must not cause idle to interrupt an awake machine.
    power.assert_awaited_once_with(False)
    assert hass.states.get("switch.decent_power").state == "on"


@pytest.mark.parametrize(
    ("state", "substate", "seconds"),
    [
        ("sleeping", "idle", 10),
        ("idle", "idle", 2),
        ("schedIdle", "idle", 2),
        ("idle", "preparingForShot", 1),
        ("heating", "idle", 1),
        ("preheating", "preparingForShot", 1),
        ("espresso", "preinfusion", 1),
        ("espresso", "pouring", 1),
        ("steam", "pouring", 1),
        ("needsWater", "idle", 1),
        (None, None, 10),
    ],
)
async def test_adaptive_machine_polling(hass, setup_entry, payloads, state, substate, seconds):
    entry, _ = setup_entry
    payloads["machine/state"] = {"state": {"state": state, "substate": substate}}
    await entry.runtime_data.machine.async_refresh()
    assert entry.runtime_data.machine.update_interval.total_seconds() == seconds
    for coordinator in (
        entry.runtime_data.workflow,
        entry.runtime_data.settings,
        entry.runtime_data.devices,
    ):
        assert coordinator.update_interval.total_seconds() == 60
    payloads["machine/state"] = {"state": {"state": "sleeping", "substate": "idle"}}
    await entry.runtime_data.machine.async_refresh()
    assert entry.runtime_data.machine.update_interval.total_seconds() == 10


async def test_adaptive_polling_backs_off_on_failure(hass, setup_entry, payloads):
    entry, _ = setup_entry
    coordinator = entry.runtime_data.machine
    payloads["machine/state"] = {"state": {"state": "espresso"}}
    await coordinator.async_refresh()
    assert coordinator.update_interval.total_seconds() == 1
    payloads["machine/state"] = DecaidError("offline")
    await coordinator.async_refresh()
    assert coordinator.update_interval.total_seconds() == 10
    assert hass.states.get("sensor.decent_state").state == "unavailable"
    payloads["machine/state"] = {"state": {"state": "idle", "substate": "idle"}}
    await coordinator.async_refresh()
    assert coordinator.update_interval.total_seconds() == 2


async def test_streams_start_and_stop_with_entry(hass, payloads, mock_stream_start, shot_payload):
    import asyncio
    import json

    from aiohttp import WSMessage, WSMsgType

    mock_stream_start.side_effect = mock_stream_start.real_start
    queues = {
        path: asyncio.Queue()
        for path in (
            "machine/snapshot",
            "devices",
            "machine/waterLevels",
            "scale/snapshot",
            "machine/shotState",
        )
    }
    sockets = {}
    for path, queue in queues.items():
        socket = AsyncMock()
        socket.receive.side_effect = queue.get
        sockets[path] = socket
    queues["machine/snapshot"].put_nowait(
        WSMessage(WSMsgType.TEXT, json.dumps({"state": {"state": "heating"}}), "")
    )
    queues["devices"].put_nowait(
        WSMessage(WSMsgType.TEXT, json.dumps({"devices": payloads["devices"]}), "")
    )
    queues["machine/waterLevels"].put_nowait(
        WSMessage(WSMsgType.TEXT, json.dumps({"currentLevel": 28.3, "refillLevel": 5}), "")
    )
    queues["scale/snapshot"].put_nowait(
        WSMessage(WSMsgType.TEXT, json.dumps({"status": "connected"}), "")
    )
    queues["scale/snapshot"].put_nowait(
        WSMessage(
            WSMsgType.TEXT,
            json.dumps({"weight": 36.2, "weightFlow": 1.5, "battery": 50, "timerValue": 25000}),
            "",
        )
    )
    queues["machine/shotState"].put_nowait(WSMessage(WSMsgType.TEXT, json.dumps(shot_payload), ""))
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.decaid.api.DecaidClient.get", side_effect=lambda path: payloads[path]
        ),
        patch(
            "custom_components.decaid.api.DecaidClient.connect_stream",
            side_effect=lambda path: sockets[path],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.decent_state").state == "heating"
        assert hass.states.get("switch.decent_power").state == "on"
        assert hass.states.get("sensor.decent_water_level").state == "28.3"
        assert hass.states.get("sensor.decent_refill_threshold").state == "5.0"
        assert hass.states.get("sensor.decent_scale_weight").state == "36.2"
        assert hass.states.get("sensor.decent_scale_weight_flow").state == "1.5"
        assert hass.states.get("sensor.decent_scale_battery").state == "50.0"
        assert hass.states.get("sensor.decent_scale_timer").state == "25000.0"
        assert (
            hass.states.get("sensor.decent_scale_timer").attributes["unit_of_measurement"] == "ms"
        )
        assert hass.states.get("sensor.decent_shot_phase").state == "idle"
        assert hass.states.get("event.decent_shot_event").state == "unknown"  # Initial replay.
        assert (
            hass.states.get("sensor.decent_water_level").attributes["unit_of_measurement"] == "mm"
        )
        tasks = [c.stream._task for c in entry.runtime_data.streams]
        assert all(task is not None and not task.done() for task in tasks)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert all(task.done() for task in tasks)
        for socket in sockets.values():
            socket.close.assert_awaited_once()


async def test_shot_events_preserve_rapid_decisions_and_ignore_replay(
    hass, setup_entry, shot_payload
):
    from time import monotonic

    from homeassistant.helpers.event import async_track_state_change_event

    entry, _ = setup_entry
    shot = entry.runtime_data.shot
    shot.stream.active = True
    shot.stream.received_at = monotonic()
    shot.async_receive(shot_payload, initial=True)
    event_id = "event.decent_shot_event"
    events = []
    unsub = async_track_state_change_event(hass, [event_id], events.append)
    try:
        for kind, reason, phase in (
            ("advance", "profileSkip", "pouring"),
            ("stop", "targetWeight", "stopping"),
            ("finalize", "futureReason", "finished"),
        ):
            shot.async_receive(
                {
                    **shot_payload,
                    "event": "decision",
                    "shotId": "shot-1",
                    "state": phase,
                    "scaleLost": True,
                    "decision": {"kind": kind, "reason": reason},
                }
            )
        await hass.async_block_till_done()
        assert [e.data["new_state"].attributes["decision"]["reason"] for e in events] == [
            "profileSkip",
            "targetWeight",
            "futureReason",
        ]
        assert hass.states.get("sensor.decent_shot_phase").state == "finished"
        assert hass.states.get("sensor.decent_last_shot_stop_reason").state == "targetWeight"
        assert hass.states.get("binary_sensor.decent_scale_lost_during_shot").state == "on"
        events.clear()
        shot.async_receive(shot.data)
        shot.async_receive(shot.data)
        await hass.async_block_till_done()
        assert len(events) == 2  # Identical consecutive events still arrive individually.
        last_event_time = hass.states.get(event_id).state
        shot.async_stream_lost()
        assert hass.states.get(event_id).state == "unavailable"
        shot.async_receive(shot_payload, initial=True)
        assert hass.states.get(event_id).state == last_event_time
        assert hass.states.get("binary_sensor.decent_scale_lost_during_shot").state == "off"
        assert hass.states.get("sensor.decent_shot_phase").state == "idle"
    finally:
        unsub()


async def test_scale_optional_values_and_disconnect(hass, setup_entry):
    from time import monotonic

    entry, _ = setup_entry
    scale = entry.runtime_data.scale
    scale.stream.active = True
    scale.stream.received_at = monotonic()
    scale.async_receive({"weight": 0, "weightFlow": 0, "battery": None, "timerValue": None})
    assert hass.states.get("sensor.decent_scale_weight").state == "0.0"
    assert hass.states.get("sensor.decent_scale_battery").state == "unknown"
    assert hass.states.get("sensor.decent_scale_timer").state == "unknown"
    scale.set_device_connected(False)
    assert hass.states.get("sensor.decent_scale_weight").state == "unavailable"
    assert hass.states.get("sensor.decent_state").state == "sleeping"
    scale.set_device_connected(True)
    assert hass.states.get("sensor.decent_scale_weight").state == "unavailable"
    scale.async_receive({"weight": 1.2, "weightFlow": 0})
    assert hass.states.get("sensor.decent_scale_weight").state == "1.2"


async def test_water_entity_disconnect_recovery_and_unchanged_threshold(
    hass, setup_entry, payloads, freezer
):
    from datetime import timedelta
    from time import monotonic

    entry, _ = setup_entry
    water = entry.runtime_data.water

    def push(level):
        water.stream.active = True
        water.stream.received_at = monotonic()
        water.async_receive({"currentLevel": level, "refillLevel": 5})

    push(28.3)
    threshold = hass.states.get("sensor.decent_refill_threshold")
    freezer.tick(timedelta(seconds=1))
    push(28.4)
    assert hass.states.get("sensor.decent_water_level").state == "28.4"
    assert (
        hass.states.get("sensor.decent_refill_threshold").last_reported == threshold.last_reported
    )
    payloads["devices"] = []
    await entry.runtime_data.devices.async_refresh()
    push(29)
    assert hass.states.get("sensor.decent_water_level").state == "unavailable"
    payloads["devices"] = [{"type": "machine", "state": "connected"}]
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get("sensor.decent_water_level").state == "unavailable"
    push(30)
    assert hass.states.get("sensor.decent_water_level").state == "30.0"
    assert hass.states.get("sensor.decent_refill_threshold").state == "5.0"
