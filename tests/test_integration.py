"""Exercise config flows and real HA entity/platform lifecycle."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.decaid.api import DecaidError
from custom_components.decaid.switch import BUSY_STATES


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
        "shots?limit=1": {"items": [], "total": 1265, "limit": 1, "offset": 0},
        "machine/info": {
            "version": "1352",
            "model": "DE1Pro",
            "serialNumber": "12345",
            "GHC": True,
            "extra": {"refillKit": False, "voltage": 230},
        },
        "machine/capabilities": {"capabilities": []},
        "machine/settings": {
            "fan": 50,
            "usb": False,
            "flushTemp": 89.3,
            "flushTimeout": 4.7,
            "flushFlow": 7.1,
            "hotWaterFlow": 6.2,
            "steamFlow": 0.81,
            "tankTemp": 31,
            "steamPurgeMode": 0,
        },
        "display": {
            "wakeLockEnabled": True,
            "wakeLockOverride": False,
            "brightness": 70,
            "requestedBrightness": 70,
            "lowBatteryBrightnessActive": False,
            "platformSupported": {"brightness": True, "wakeLock": True},
        },
        "presence/settings": {
            "userPresenceEnabled": True,
            "sleepTimeoutMinutes": 30,
            "keepAwakeUntil": None,
            "schedules": [],
        },
        "update": {
            "phase": "idle",
            "currentVersion": "1.4.0",
            "latestVersion": None,
            "releaseNotes": None,
            "releaseUrl": None,
            "installable": False,
            "progress": None,
            "error": None,
        },
        "machine/waterLevels": {"currentLevel": 28.3, "refillLevel": 5},
        "machine/firmware": {
            "artifacts": [{"id": "de1-1358", "build": 1358, "versionLabel": "1358"}],
            "machine": {"model": "DE1Pro", "build": 1352},
            "recommendedArtifactId": "de1-1358",
            "updateAvailable": True,
            "operation": {"state": "idle"},
        },
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
    assert len(hass.states.async_all()) == 57
    assert hass.states.get("number.decent_target_steam_temperature").state == "unavailable"
    # Water levels load over REST before their stream connects.
    assert hass.states.get("sensor.decent_water_level").state == "28.3"
    assert hass.states.get("number.decent_refill_threshold").state == "5"
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
    # Sleeping a busy machine would abort the shot.
    with pytest.raises(HomeAssistantError, match="busy"):
        await call("turn_off")
    power.assert_not_awaited()
    payloads["machine/state"] = {"state": {"state": "idle"}}
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
    assert hass.states.get("binary_sensor.decent_machine_connected").state == "unavailable"
    # A valid machine state read still proves the machine is there.
    assert hass.states.get("switch.decent_power").state == "off"


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
    hass.config_entries.async_update_entry(entry, title="Decent (192.168.2.231)")
    result = await hass.config_entries.flow.async_init(
        "decaid", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.232", "port": 8080}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["host"] == "192.168.2.232"
    assert entry.title == "Decent (192.168.2.232)"
    assert hass.states.get(original_id) is not None


async def test_reconfigure_keeps_custom_title(hass, setup_entry):
    entry, _ = setup_entry
    hass.config_entries.async_update_entry(entry, title="Kitchen espresso")
    result = await hass.config_entries.flow.async_init(
        "decaid", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.233", "port": 8080}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.title == "Kitchen espresso"


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
        assert hass.states.get("switch.decent_power").state == "off"
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


async def test_streams_start_and_stop_with_entry(
    hass, payloads, mock_stream_start, shot_payload, shot_settings_payload
):
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
            "machine/shotSettings",
            "update",
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
    queues["machine/shotSettings"].put_nowait(
        WSMessage(WSMsgType.TEXT, json.dumps(shot_settings_payload), "")
    )
    queues["update"].put_nowait(
        WSMessage(
            WSMsgType.TEXT,
            json.dumps(
                {
                    **payloads["update"],
                    "phase": "downloading",
                    "latestVersion": "1.5.0",
                    "progress": 0.25,
                }
            ),
            "",
        )
    )
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
        assert hass.states.get("number.decent_refill_threshold").state == "5"
        assert hass.states.get("sensor.decent_scale_weight").state == "36.2"
        assert hass.states.get("sensor.decent_scale_weight_flow").state == "1.5"
        assert hass.states.get("sensor.decent_scale_battery").state == "50.0"
        assert hass.states.get("sensor.decent_scale_timer").state == "25000.0"
        assert (
            hass.states.get("sensor.decent_scale_timer").attributes["unit_of_measurement"] == "ms"
        )
        assert hass.states.get("sensor.decent_shot_phase").state == "idle"
        for suffix, expected, unit in (
            ("target_steam_temperature", "160", "°C"),
            ("target_steam_duration", "120", "s"),
            ("target_hot_water_temperature", "88", "°C"),
            ("target_hot_water_volume", "50", "mL"),
            ("target_hot_water_duration", "30", "s"),
            ("target_shot_volume", "200", "mL"),
            ("configured_group_temperature", "93.0", "°C"),
        ):
            state = hass.states.get(f"number.decent_{suffix}")
            assert state.state == expected
            assert state.attributes["unit_of_measurement"] == unit
        assert hass.states.get("event.decent_shot_event").state == "unknown"  # Initial replay.
        app = hass.states.get("update.decent_decaid_app")
        assert app.attributes["latest_version"] == "1.5.0"
        assert app.attributes["update_percentage"] == 25
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
    # A bare builtin is not a callback, so Home Assistant would run it in the
    # executor and the events could arrive out of order.
    unsub = async_track_state_change_event(
        hass, [event_id], callback(lambda event: events.append(event))
    )
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
    threshold = hass.states.get("number.decent_refill_threshold")
    freezer.tick(timedelta(seconds=1))
    push(28.4)
    assert hass.states.get("sensor.decent_water_level").state == "28.4"
    assert (
        hass.states.get("number.decent_refill_threshold").last_reported == threshold.last_reported
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
    assert hass.states.get("number.decent_refill_threshold").state == "5"


async def test_shot_settings_follow_machine_availability(
    hass, setup_entry, payloads, shot_settings_payload
):
    from time import monotonic

    entry, _ = setup_entry
    settings = entry.runtime_data.shot_settings

    def push():
        settings.stream.active = True
        settings.stream.received_at = monotonic()
        settings.async_receive(shot_settings_payload)

    push()
    entity_id = "number.decent_target_steam_temperature"
    assert hass.states.get(entity_id).state == "160"
    payloads["devices"] = []
    await entry.runtime_data.devices.async_refresh()
    push()
    assert hass.states.get(entity_id).state == "unavailable"
    payloads["devices"] = [{"type": "machine", "state": "connected"}]
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get(entity_id).state == "unavailable"
    push()
    assert hass.states.get(entity_id).state == "160"


def _get_from(payloads):
    async def get(path):
        result = payloads[path]
        if isinstance(result, Exception):
            raise result
        return result

    return get


@pytest.mark.parametrize("state", sorted(BUSY_STATES))
async def test_sleep_refused_while_busy(hass, setup_entry, payloads, state):
    entry, power = setup_entry
    payloads["machine/state"] = {"state": {"state": state}}
    await entry.runtime_data.machine.async_refresh()
    with pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.decent_power"}, blocking=True
        )
    power.assert_not_awaited()
    assert hass.states.get("switch.decent_power").state == "on"


async def test_sleep_not_resent_to_sleeping_machine(hass, setup_entry):
    _, power = setup_entry
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.decent_power"}, blocking=True
    )
    power.assert_not_awaited()
    assert hass.states.get("switch.decent_power").state == "off"


async def test_setup_when_only_device_inventory_answers(hass, payloads):
    """A machine that is off at the wall must not block the tablet's entities."""
    payloads["machine/state"] = DecaidError("no machine")
    payloads["devices"] = [{"type": "machine", "id": "abc", "state": "disconnected"}]
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=_get_from(payloads)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.decent_state").state == "unavailable"
        assert hass.states.get("switch.decent_power").state == "unavailable"
        assert hass.states.get("binary_sensor.decent_machine_connected").state == "off"
        assert hass.states.get("sensor.decent_tablet_battery").state == "80.0"
        assert hass.states.get("sensor.decent_last_shot").state == "unknown"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_live_readings_rounded_and_flow_class(hass, setup_entry, payloads):
    entry, _ = setup_entry
    payloads["machine/state"] = {
        **payloads["machine/state"],
        "groupTemperature": 92.4449,
        "pressure": 9.876,
        "flow": 2.0412,
    }
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get("sensor.decent_grouphead_temperature").state == "92.4"
    assert hass.states.get("sensor.decent_pressure").state == "9.9"
    flow = hass.states.get("sensor.decent_flow")
    assert flow.state == "2.0"
    assert flow.attributes["unit_of_measurement"] == "mL/s"
    assert flow.attributes["device_class"] == "volume_flow_rate"
    # Jitter below the kept precision does not write a new state.
    before = hass.states.get("sensor.decent_grouphead_temperature").last_reported
    payloads["machine/state"] = {**payloads["machine/state"], "groupTemperature": 92.4101}
    await entry.runtime_data.machine.async_refresh()
    assert hass.states.get("sensor.decent_grouphead_temperature").last_reported == before


async def test_diagnostic_entity_categories(hass, setup_entry):
    from homeassistant.const import EntityCategory
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity_id in (
        "sensor.decent_tablet_battery",
        "sensor.decent_scale_battery",
        "binary_sensor.decent_machine_connected",
        "binary_sensor.decent_scale_connected",
    ):
        assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
    assert registry.async_get("sensor.decent_pressure").entity_category is None


async def test_last_stop_reason_survives_restart(hass, payloads, shot_payload):
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(hass, [State("sensor.decent_last_shot_stop_reason", "targetWeight")])
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=_get_from(payloads)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        shot = entry.runtime_data.shot
        shot.stream.active = True
        shot.async_receive(shot_payload, initial=True)
        assert hass.states.get("sensor.decent_last_shot_stop_reason").state == "targetWeight"
        shot.async_receive(
            {**shot_payload, "event": "decision", "decision": {"kind": "stop", "reason": "time"}}
        )
        assert hass.states.get("sensor.decent_last_shot_stop_reason").state == "time"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_event_missed_during_reconnect_is_delivered(hass, setup_entry, shot_payload):
    entry, _ = setup_entry
    shot = entry.runtime_data.shot
    event_id = "event.decent_shot_event"
    shot.stream.active = True
    shot.async_receive(shot_payload, initial=True)  # Startup replay: never an event.
    assert hass.states.get(event_id).state == "unknown"
    # The same frame replayed after a reconnect is still not an event.
    shot.async_stream_lost()
    shot.async_receive(shot_payload, initial=True)
    assert hass.states.get(event_id).state == "unknown"
    # A newer frame on reconnect happened while the socket was down.
    shot.async_stream_lost()
    shot.async_receive(
        {
            **shot_payload,
            "event": "terminal",
            "state": "finished",
            "timestamp": "2026-09-10T07:19:40.000000",
            "decision": {"kind": "terminal", "reason": "targetWeight"},
        },
        initial=True,
    )
    state = hass.states.get(event_id)
    assert state.state not in ("unknown", "unavailable")
    assert state.attributes["event_type"] == "terminal"


async def test_scale_reading_restores_connection(hass, setup_entry):
    from time import monotonic

    entry, _ = setup_entry
    scale = entry.runtime_data.scale
    scale.stream.active = True
    scale.stream.received_at = monotonic()
    scale.set_device_connected(False)
    scale.async_receive({"weight": 1.2, "weightFlow": 0})
    assert scale.device_connected
    assert hass.states.get("sensor.decent_scale_weight").state == "1.2"


async def test_diagnostics_redact_private_details(hass, setup_entry):
    from custom_components.decaid.diagnostics import async_get_config_entry_diagnostics

    entry, _ = setup_entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, "machine_id": "abc"})
    entry.runtime_data.settings.last_exception = DecaidError(
        "Unable to read settings: cannot connect to 192.168.2.231:8080"
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = str(result)
    assert "192.168.2.231" not in text
    assert "abc" not in text  # Device ID, in the inventory and the configured machine ID.
    assert result["entry"]["machine_id"] == "**REDACTED**"
    assert result["coordinators"]["devices"]["data"][0]["state"] == "connected"
    assert result["coordinators"]["settings"]["data"] == ["chargingState"]
    assert result["coordinators"]["shots"]["data"] == {
        "total": 1265,
        "latest_fields": None,
        "summary": None,
    }
    assert "12345" not in text  # Machine serial number.
    assert result["coordinators"]["machine_info"]["data"]["model"] == "DE1Pro"
    assert result["coordinators"]["presence"]["data"]["schedules"] == 0
    assert result["coordinators"]["machine"]["data"]["groupTemperature"] == 92.4


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.2.231", "192.168.2.231"),
        (" ::1 ", "::1"),
        ("Decent-Tablet.local.", "decent-tablet.local"),
        ("tablet", "tablet"),
        ("http://tablet", None),
        ("tablet:8080", None),
        ("[::1]", None),
        ("-bad.local", None),
        ("bad_host", None),
        ("", None),
    ],
)
def test_normalize_host(raw, expected):
    from custom_components.decaid.config_flow import normalize_host

    assert normalize_host(raw) == expected


async def test_flow_hostname_and_devices_only(hass):
    async def get(path):
        if path == "devices":
            return [{"type": "machine", "id": "abc", "state": "disconnected"}]
        raise DecaidError("no machine")

    result = await hass.config_entries.flow.async_init("decaid", context={"source": SOURCE_USER})
    with (
        patch("custom_components.decaid.api.DecaidClient.get", side_effect=get),
        patch("custom_components.decaid.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": " Decent-Tablet.local ", "port": 8080, "machine_id": "abc"}
        )
        assert result["type"] == "create_entry"
        assert result["data"]["host"] == "decent-tablet.local"
        # The machine is away, so there is no serial to identify it by yet.
        assert result["result"].unique_id is None
        await hass.async_block_till_done()


async def test_flow_rejects_unknown_machine_id(hass, payloads):
    result = await hass.config_entries.flow.async_init("decaid", context={"source": SOURCE_USER})
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=_get_from(payloads)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.2.231", "port": 8080, "machine_id": "typo"}
        )
    assert result["errors"] == {"machine_id": "unknown_machine"}


async def test_setup_records_machine_serial(hass, setup_entry):
    entry, _ = setup_entry
    assert entry.unique_id == "12345"


async def test_new_tablet_for_same_machine_moves_entry(hass, setup_entry, payloads):
    entry, _ = setup_entry
    power_id = hass.states.get("switch.decent_power").entity_id
    result = await hass.config_entries.flow.async_init("decaid", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.240", "port": 8081}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "moved_to_new_tablet"
    assert len(hass.config_entries.async_entries("decaid")) == 1
    assert (entry.data["host"], entry.data["port"]) == ("192.168.2.240", 8081)
    assert hass.states.get(power_id) is not None


async def test_serial_recorded_once_machine_reports_it(hass, payloads):
    real = payloads["machine/info"]
    payloads["machine/info"] = {**real, "serialNumber": "0", "model": "Unknown"}
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=_get_from(payloads)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Decaid's placeholder before the machine's handshake is not a serial.
        assert entry.unique_id is None
        payloads["machine/info"] = real
        await entry.runtime_data.machine_info.async_refresh()
        assert entry.unique_id == "12345"
        # Once set it is kept, even if a later read differs.
        payloads["machine/info"] = {**real, "serialNumber": "67890"}
        await entry.runtime_data.machine_info.async_refresh()
        assert entry.unique_id == "12345"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_onto_another_entrys_machine_is_refused(hass, setup_entry, payloads):
    other = MockConfigEntry(
        domain="decaid", unique_id="99999", data={"host": "192.168.2.250", "port": 8080}
    )
    other.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "decaid", context={"source": SOURCE_RECONFIGURE, "entry_id": other.entry_id}
    )
    # The setup_entry tablet's machine (serial 12345) already has its own entry.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.251", "port": 8080}
    )
    assert result["reason"] == "machine_already_configured"
    assert other.data["host"] == "192.168.2.250"


async def test_serial_ignored_for_another_selected_machine(hass, setup_entry, payloads):
    """machine/info describes the connected machine, not a listed one the entry selects."""
    entry, _ = setup_entry
    payloads["devices"] = [
        {"type": "machine", "id": "a", "state": "connected"},
        {"type": "machine", "id": "b", "state": "disconnected"},
    ]
    result = await hass.config_entries.flow.async_init("decaid", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.240", "port": 8080, "machine_id": "b"}
    )
    await hass.async_block_till_done()
    # Machine A's existing entry is left alone; B gets an entry of its own.
    assert result["type"] == "create_entry"
    assert result["result"].unique_id is None
    assert entry.data["host"] == "192.168.2.231"


async def test_serial_not_recorded_while_selected_machine_is_away(hass, payloads):
    payloads["devices"] = [
        {"type": "machine", "id": "a", "state": "connected"},
        {"type": "machine", "id": "b", "state": "disconnected"},
    ]
    entry = MockConfigEntry(
        domain="decaid", data={"host": "192.168.2.231", "port": 8080, "machine_id": "b"}
    )
    entry.add_to_hass(hass)
    with patch("custom_components.decaid.api.DecaidClient.get", side_effect=_get_from(payloads)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.unique_id is None
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_without_serial_clears_identity(hass, setup_entry, payloads):
    entry, _ = setup_entry
    assert entry.unique_id == "12345"
    payloads["machine/info"] = DecaidError("machine away")
    payloads["machine/state"] = DecaidError("machine away")
    payloads["devices"] = []
    result = await hass.config_entries.flow.async_init(
        "decaid", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.2.232", "port": 8080}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    # The new tablet's machine is unknown, so the old serial must not follow.
    assert entry.unique_id is None


async def test_late_serial_collision_raises_repair_issue(hass, setup_entry, payloads):
    from homeassistant.helpers import issue_registry as ir

    original, _ = setup_entry
    real = payloads["machine/info"]
    payloads["machine/info"] = {**real, "serialNumber": "0"}
    replacement = MockConfigEntry(
        domain="decaid", title="New tablet", data={"host": "192.168.2.240", "port": 8080}
    )
    replacement.add_to_hass(hass)
    assert await hass.config_entries.async_setup(replacement.entry_id)
    await hass.async_block_till_done()
    assert replacement.unique_id is None
    payloads["machine/info"] = real
    await replacement.runtime_data.machine_info.async_refresh()
    # Neither entry is merged or removed automatically.
    assert replacement.unique_id is None
    assert original.unique_id == "12345"
    issue = ir.async_get(hass).async_get_issue(
        "decaid", f"duplicate_machine_{replacement.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["existing"] == original.title
    assert await hass.config_entries.async_remove(replacement.entry_id)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue("decaid", f"duplicate_machine_{replacement.entry_id}")
        is None
    )


async def test_refill_threshold_rereads_while_stream_is_down(hass, setup_entry, payloads):
    entry, _ = setup_entry
    water = entry.runtime_data.water
    assert not water.stream.active
    with patch(
        "custom_components.decaid.api.DecaidClient.command", new_callable=AsyncMock
    ) as command:
        payloads["machine/waterLevels"] = {"currentLevel": 28.3, "refillLevel": 8}
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": "number.decent_refill_threshold", "value": 8},
            blocking=True,
        )
        await hass.async_block_till_done()
    command.assert_awaited_once_with("POST", "machine/waterLevels", {"refillLevel": 8})
    assert hass.states.get("number.decent_refill_threshold").state == "8"
