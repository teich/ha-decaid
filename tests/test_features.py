"""Device info, settings controls, Bengle extras, updates, and the last shot."""

from time import monotonic
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.decaid.api import DecaidError
from custom_components.decaid.coordinator import summarize_shot
from tests.test_integration import payloads  # noqa: F401  (shared fixture)

SHOT = {
    "id": "shot-1",
    "timestamp": "2026-11-20T08:15:00.000",
    "workflow": {
        "profile": {"title": "Adaptive"},
        "context": {
            "targetDoseWeight": 18,
            "targetYield": 36,
            "grinderModel": "Niche",
            "grinderSetting": "12",
            "coffeeName": "House",
        },
    },
    "annotations": {"actualDoseWeight": 18.2, "enjoyment": 80},
    "stopReason": "targetWeight",
}

MEASUREMENTS = [
    {
        "machine": {"timestamp": "2026-11-20T08:15:00.000", "pressure": 2.0, "flow": 4.0},
        "scale": {"weight": 0.0},
    },
    {
        "machine": {"timestamp": "2026-11-20T08:15:10.000", "pressure": 9.04, "flow": 2.0},
        "scale": {"weight": 20.0},
    },
    {
        "machine": {"timestamp": "2026-11-20T08:15:30.500", "pressure": 6.0, "flow": 2.5},
        "scale": {"weight": 36.44},
    },
]

BENGLE = {
    "capabilities": [
        "cupWarmer",
        "integratedScale",
        "stopAtWeight",
        "ledStrip",
        "scaleCalibration",
        "preheat",
        "wakeSchedule",
    ]
}


@pytest.fixture
async def setup(hass, payloads):  # noqa: F811
    async def get(path):
        result = payloads[path]
        if isinstance(result, Exception):
            raise result
        return result

    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.decaid.api.DecaidClient.get", side_effect=get),
        patch(
            "custom_components.decaid.api.DecaidClient.command", new_callable=AsyncMock
        ) as command,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, command
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def test_device_info_from_machine(hass, setup, payloads):  # noqa: F811
    entry, _ = setup
    device = _device(hass, entry)
    assert (device.model, device.serial_number, device.sw_version) == ("DE1Pro", "12345", "1352")
    assert device.manufacturer == "Decent Espresso"
    assert hass.states.get("binary_sensor.decent_refill_kit_detected").state == "off"
    # Placeholders from before the machine's handshake never replace real values.
    payloads["machine/info"] = {"version": "0", "model": "Unknown", "serialNumber": "0"}
    await entry.runtime_data.machine_info.async_refresh()
    device = _device(hass, entry)
    assert device.model == "DE1Pro"


async def test_device_without_machine_info(hass, payloads):  # noqa: F811
    payloads["machine/info"] = DecaidError("offline")
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.decaid.api.DecaidClient.get",
        side_effect=lambda path: _raise_or_return(payloads[path]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device = _device(hass, entry)
        assert device.model == "Decaid"
        assert hass.states.get("binary_sensor.decent_refill_kit_detected").state == "unavailable"
        await hass.config_entries.async_unload(entry.entry_id)


def _device(hass, entry):
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):
        return registry.async_get_device_by_identifier(("decaid", entry.entry_id), entry.entry_id)
    return registry.async_get_device(identifiers={("decaid", entry.entry_id)})


def _raise_or_return(result):
    if isinstance(result, Exception):
        raise result
    return result


async def test_shot_settings_number_resends_all_settings(hass, setup, shot_settings_payload):
    entry, command = setup
    settings = entry.runtime_data.shot_settings
    settings.stream.active = True
    settings.stream.received_at = monotonic()
    settings.async_receive(shot_settings_payload)
    await call(hass, "number", "set_value", "number.decent_target_steam_temperature", value=150)
    command.assert_awaited_once_with(
        "POST", "machine/shotSettings", {**shot_settings_payload, "targetSteamTemp": 150}
    )
    command.reset_mock()
    await call(
        hass, "number", "set_value", "number.decent_configured_group_temperature", value=92.5
    )
    assert command.await_args.args[2]["groupTemp"] == 92.5
    assert command.await_args.args[2]["targetSteamTemp"] == 160


async def test_shot_settings_number_needs_a_snapshot(hass, setup):
    _, command = setup
    # Home Assistant skips unavailable entities, so nothing is sent.
    assert hass.states.get("number.decent_target_steam_temperature").state == "unavailable"
    await call(hass, "number", "set_value", "number.decent_target_steam_temperature", value=150)
    command.assert_not_awaited()


async def test_setting_numbers(hass, setup):
    entry, command = setup
    water = entry.runtime_data.water
    water.stream.active = True
    water.stream.received_at = monotonic()
    water.async_receive({"currentLevel": 28.3, "refillLevel": 5})
    for entity_id, value, expected in (
        (
            "number.decent_refill_threshold",
            7.4,
            ("POST", "machine/waterLevels", {"refillLevel": 7}),
        ),
        ("number.decent_fan_threshold", 45, ("POST", "machine/settings", {"fan": 45})),
        ("number.decent_steam_flow", 0.9, ("POST", "machine/settings", {"steamFlow": 0.9})),
        (
            "number.decent_tablet_screen_brightness",
            40,
            ("PUT", "display/brightness", {"brightness": 40}),
        ),
        (
            "number.decent_sleep_timeout",
            60,
            ("POST", "presence/settings", {"sleepTimeoutMinutes": 60}),
        ),
    ):
        command.reset_mock()
        await call(hass, "number", "set_value", entity_id, value=value)
        assert command.await_args.args == expected, entity_id
    assert hass.states.get("number.decent_fan_threshold").state == "50"
    assert hass.states.get("number.decent_tablet_screen_brightness").state == "70"
    assert hass.states.get("number.decent_sleep_timeout").state == "30"


async def test_switches_select_and_buttons(hass, setup):
    _, command = setup
    assert hass.states.get("switch.decent_usb_charger").state == "off"
    await call(hass, "switch", "turn_on", "switch.decent_usb_charger")
    command.assert_awaited_with("POST", "machine/settings", {"usb": "enable"})
    await call(hass, "switch", "turn_on", "switch.decent_tablet_screen_keep_awake")
    command.assert_awaited_with("POST", "display/wakelock", None)
    await call(hass, "switch", "turn_off", "switch.decent_tablet_screen_keep_awake")
    command.assert_awaited_with("DELETE", "display/wakelock", None)
    await call(hass, "switch", "turn_off", "switch.decent_presence_detection")
    command.assert_awaited_with("POST", "presence/settings", {"userPresenceEnabled": False})

    assert hass.states.get("select.decent_steam_purge_mode").state == "normal"
    await call(
        hass, "select", "select_option", "select.decent_steam_purge_mode", option="two_tap_stop"
    )
    command.assert_awaited_with("POST", "machine/settings", {"steamPurgeMode": 1})

    await call(hass, "button", "press", "button.decent_tare_scale")
    command.assert_awaited_with("PUT", "scale/tare", None)
    await call(hass, "button", "press", "button.decent_reset_scale_timer")
    command.assert_awaited_with("PUT", "scale/timer/reset", None)
    await call(hass, "button", "press", "button.decent_signal_presence")
    command.assert_awaited_with("POST", "machine/heartbeat", None)


async def test_command_failure_is_reported(hass, setup):
    _, command = setup
    command.side_effect = DecaidError("HTTP 503")
    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        await call(hass, "button", "press", "button.decent_tare_scale")


async def test_scale_buttons_follow_scale_connection(hass, setup, payloads):  # noqa: F811
    entry, _ = setup
    assert hass.states.get("button.decent_tare_scale").state == "unknown"
    payloads["devices"] = [{"type": "machine", "id": "abc", "state": "connected"}]
    await entry.runtime_data.devices.async_refresh()
    assert hass.states.get("button.decent_tare_scale").state == "unavailable"


async def test_unsupported_display_features(hass, payloads):  # noqa: F811
    payloads["display"] = {
        **payloads["display"],
        "platformSupported": {"brightness": False, "wakeLock": False},
    }
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.decaid.api.DecaidClient.get",
        side_effect=lambda path: _raise_or_return(payloads[path]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("number.decent_tablet_screen_brightness").state == "unavailable"
        assert hass.states.get("switch.decent_tablet_screen_keep_awake").state == "unavailable"
        await hass.config_entries.async_unload(entry.entry_id)


async def test_update_entities(hass, setup, payloads):  # noqa: F811
    entry, _ = setup
    app = hass.states.get("update.decent_decaid_app")
    assert app.state == "off"
    assert app.attributes["installed_version"] == "1.4.0"
    # Never installs from Home Assistant.
    assert not app.attributes["supported_features"] & UpdateEntityFeature.INSTALL
    firmware = hass.states.get("update.decent_machine_firmware")
    assert firmware.state == "on"
    assert firmware.attributes["installed_version"] == "1352"
    assert firmware.attributes["latest_version"] == "1358"
    payloads["update"] = {
        **payloads["update"],
        "phase": "downloading",
        "latestVersion": "1.5.0",
        "progress": 0.4,
    }
    await entry.runtime_data.app_update.async_refresh()
    app = hass.states.get("update.decent_decaid_app")
    assert app.state == "on"
    assert app.attributes["in_progress"] is True
    assert app.attributes["update_percentage"] == 40
    payloads["machine/firmware"] = {**payloads["machine/firmware"], "machine": None}
    await entry.runtime_data.firmware.async_refresh()
    assert hass.states.get("update.decent_machine_firmware").state == "unavailable"


async def test_last_shot(hass, payloads):  # noqa: F811
    payloads["shots?limit=1"] = {"items": [SHOT], "total": 7, "limit": 1, "offset": 0}
    payloads["shots/shot-1"] = {**SHOT, "measurements": MEASUREMENTS}
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    get = AsyncMock(side_effect=lambda path: _raise_or_return(payloads[path]))
    with patch("custom_components.decaid.api.DecaidClient.get", get):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get("sensor.decent_last_shot")
        assert state.state.startswith("2026-11-20T")
        assert state.attributes["profile"] == "Adaptive"
        assert state.attributes["dose"] == 18.2
        assert state.attributes["yield"] == 36.4
        assert state.attributes["ratio"] == 2.0
        assert state.attributes["duration"] == 30.5
        assert state.attributes["peak_pressure"] == 9.0
        assert state.attributes["grinder_setting"] == "12"
        assert state.attributes["stop_reason"] == "targetWeight"
        # The full record is read once per shot, not on every poll.
        await entry.runtime_data.shots.async_refresh()
        assert [c.args[0] for c in get.await_args_list].count("shots/shot-1") == 1
        await hass.config_entries.async_unload(entry.entry_id)


async def test_last_shot_without_history(hass, setup):
    assert hass.states.get("sensor.decent_last_shot").state == "unknown"


async def test_last_shot_refreshes_after_terminal_event(hass, setup, shot_payload, freezer):
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry, _ = setup
    shots = entry.runtime_data.shots
    shot = entry.runtime_data.shot
    shot.stream.active = True
    shot.stream.received_at = monotonic()
    shot.async_receive(shot_payload, initial=True)
    with patch.object(shots, "async_request_refresh", AsyncMock()) as refresh:
        shot.async_receive({**shot_payload, "event": "terminal", "timestamp": "2026-11-22T08:00"})
        await hass.async_block_till_done()
        refresh.assert_not_awaited()
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        refresh.assert_awaited_once()


def test_summarize_shot():
    assert summarize_shot({"measurements": MEASUREMENTS}) == {
        "duration": 30.5,
        "peak_pressure": 9.0,
        "peak_flow": 4.0,
        "final_weight": 36.4,
    }
    assert summarize_shot({}) == {}
    assert summarize_shot({"measurements": [{"machine": {"pressure": float("nan")}}]}) == {}


async def test_moved_sensors_are_removed(hass, payloads):  # noqa: F811
    entry = MockConfigEntry(domain="decaid", data={"host": "192.168.2.231", "port": 8080})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", "decaid", f"{entry.entry_id}_target_steam_temperature", config_entry=entry
    )
    dropped = [
        registry.async_get_or_create(
            "sensor", "decaid", f"{entry.entry_id}_{key}", config_entry=entry
        )
        for key in ("shot_count", "steam_count")
    ]
    with patch(
        "custom_components.decaid.api.DecaidClient.get",
        side_effect=lambda path: _raise_or_return(payloads[path]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(old.entity_id) is None
        assert all(registry.async_get(e.entity_id) is None for e in dropped)
        assert registry.async_get_entity_id(
            "number", "decaid", f"{entry.entry_id}_target_steam_temperature"
        )
        await hass.config_entries.async_unload(entry.entry_id)


async def test_plain_de1_has_no_bengle_entities(hass, setup):
    assert hass.states.get("climate.decent_cup_warmer") is None
    assert hass.states.get("switch.decent_cup_warmer_pre_warm") is None


async def test_bengle_entities(hass, setup, payloads):  # noqa: F811
    entry, command = setup
    payloads["machine/cupWarmer"] = {"temperature": 55, "enabled": True, "currentTemperature": 48.5}
    payloads["machine/cupWarmer/preheat"] = {"enabled": False, "leadMinutes": 30, "active": False}
    # The machine reports its capabilities after setup, e.g. once it connects.
    payloads["machine/capabilities"] = BENGLE
    await entry.runtime_data.capabilities.async_refresh()
    await hass.async_block_till_done()

    climate = hass.states.get("climate.decent_cup_warmer")
    assert climate.state == "heat"
    assert climate.attributes["temperature"] == 55
    assert climate.attributes["current_temperature"] == 48.5
    assert hass.states.get("switch.decent_cup_warmer_pre_warm").state == "off"
    assert hass.states.get("number.decent_cup_warmer_pre_warm_lead_time").state == "30"
    assert hass.states.get("binary_sensor.decent_cup_warmer_pre_warming").state == "off"

    await call(hass, "climate", "set_temperature", "climate.decent_cup_warmer", temperature=60)
    command.assert_awaited_with("PUT", "machine/cupWarmer", {"temperature": 60, "enabled": True})
    await call(hass, "climate", "set_hvac_mode", "climate.decent_cup_warmer", hvac_mode="off")
    command.assert_awaited_with("PUT", "machine/cupWarmer", {"enabled": False})
    await call(hass, "switch", "turn_on", "switch.decent_cup_warmer_pre_warm")
    command.assert_awaited_with("PUT", "machine/cupWarmer/preheat", {"enabled": True})
    await call(hass, "number", "set_value", "number.decent_cup_warmer_pre_warm_lead_time", value=45)
    command.assert_awaited_with("PUT", "machine/cupWarmer/preheat", {"leadMinutes": 45})

    # Entities are added once; a later capability refresh does not duplicate them.
    count = len(hass.states.async_all())
    await entry.runtime_data.capabilities.async_refresh()
    await hass.async_block_till_done()
    assert len(hass.states.async_all()) == count


async def test_attribute_only_changes_are_written(hass, setup, payloads):  # noqa: F811
    """Climate temperatures and update progress change while the state does not."""
    entry, _ = setup
    payloads["machine/cupWarmer"] = {"temperature": 55, "enabled": True, "currentTemperature": 40.0}
    payloads["machine/cupWarmer/preheat"] = {"enabled": False, "leadMinutes": 30, "active": False}
    payloads["machine/capabilities"] = BENGLE
    await entry.runtime_data.capabilities.async_refresh()
    await hass.async_block_till_done()
    climate = hass.states.get("climate.decent_cup_warmer")
    assert climate.attributes["current_temperature"] == 40.0
    assert climate.attributes["hvac_action"] == "heating"
    payloads["machine/cupWarmer"] = {"temperature": 55, "enabled": True, "currentTemperature": 55.5}
    await entry.runtime_data.cup_warmer.async_refresh()
    climate = hass.states.get("climate.decent_cup_warmer")
    assert climate.state == "heat"
    assert climate.attributes["current_temperature"] == 55.5
    # At temperature: the mat is holding, not heating.
    assert climate.attributes["hvac_action"] == "idle"

    base = {**payloads["update"], "phase": "downloading", "latestVersion": "1.5.0"}
    payloads["update"] = {**base, "progress": 0.4}
    await entry.runtime_data.app_update.async_refresh()
    assert hass.states.get("update.decent_decaid_app").attributes["update_percentage"] == 40
    payloads["update"] = {**base, "progress": 0.8}
    await entry.runtime_data.app_update.async_refresh()
    assert hass.states.get("update.decent_decaid_app").attributes["update_percentage"] == 80


async def test_app_update_survives_failed_download(hass, setup, payloads):  # noqa: F811
    entry, _ = setup
    # Decaid keeps latestVersion while it knows of a newer release.
    payloads["update"] = {**payloads["update"], "phase": "error", "latestVersion": "1.5.0"}
    await entry.runtime_data.app_update.async_refresh()
    app = hass.states.get("update.decent_decaid_app")
    assert app.state == "on"
    assert app.attributes["latest_version"] == "1.5.0"
    assert app.attributes["in_progress"] is False


async def test_firmware_compares_builds_not_labels(hass, setup, payloads):  # noqa: F811
    entry, _ = setup
    payloads["machine/firmware"] = {
        **payloads["machine/firmware"],
        "artifacts": [{"id": "de1-1358", "build": 1358, "versionLabel": "v1.4"}],
    }
    await entry.runtime_data.firmware.async_refresh()
    firmware = hass.states.get("update.decent_machine_firmware")
    assert firmware.state == "on"
    assert firmware.attributes["latest_version"] == "1358"
    assert firmware.attributes["title"] == "Firmware v1.4"
