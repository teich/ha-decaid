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
    assert len(hass.states.async_all()) == 18
    assert hass.states.get("sensor.decent_grouphead_temperature").state == "92.4"
    assert hass.states.get("sensor.decent_target_dose").state == "18.0"
    assert hass.states.get("binary_sensor.decent_machine_connected").state == "on"
    assert hass.states.get("switch.decent_power").state == "off"
    assert entry.runtime_data.machine.update_interval.total_seconds() == 10
    assert entry.runtime_data.settings.update_interval.total_seconds() == 60


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
