"""Decaid integration for Decent Espresso machines."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DecaidClient
from .const import CONF_MACHINE_ID, PLATFORMS
from .coordinator import (
    DecaidCoordinator,
    DecaidData,
    DecaidPushCoordinator,
    DecaidShotCoordinator,
    DecaidShotSettingsCoordinator,
    DecaidStreamCoordinator,
    DecaidWaterCoordinator,
)

type DecaidConfigEntry = ConfigEntry[DecaidData]


async def async_setup_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Set up polling and platforms."""
    client = DecaidClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    data = DecaidData(
        client,
        DecaidPushCoordinator(hass, client, "machine/state", 10, "machine/snapshot"),
        DecaidCoordinator(hass, client, "workflow", 60),
        DecaidCoordinator(hass, client, "settings", 60),
        DecaidPushCoordinator(hass, client, "devices", 60, "devices"),
        DecaidWaterCoordinator(hass, client),
        DecaidStreamCoordinator(hass, client, "scale/snapshot"),
        DecaidShotCoordinator(hass, client),
        DecaidShotSettingsCoordinator(hass, client),
    )
    await data.machine.async_config_entry_first_refresh()
    # Optional resources can recover after setup instead of blocking all entities.
    await asyncio.gather(*(c.async_refresh() for c in (data.workflow, data.settings, data.devices)))
    entry.runtime_data = data

    @callback
    def devices_updated():
        if data.devices.last_update_success:
            connected = data.machine_connected(entry.data.get(CONF_MACHINE_ID))
            data.machine.set_device_connected(connected)
            data.water.set_device_connected(connected)
            data.shot_settings.set_device_connected(connected)

    entry.async_on_unload(data.devices.async_add_listener(devices_updated))
    devices_updated()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for coordinator in data.streams:
        coordinator.start(entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Unload platforms and their coordinator listeners."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await asyncio.gather(*(coordinator.stop() for coordinator in entry.runtime_data.streams))
    return True
