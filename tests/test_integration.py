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
