"""Decaid integration for Decent Espresso machines."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DecaidClient
from .const import PLATFORMS
from .coordinator import DecaidCoordinator, DecaidData

type DecaidConfigEntry = ConfigEntry[DecaidData]


async def async_setup_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Set up polling and platforms."""
    client = DecaidClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    data = DecaidData(
        client,
        DecaidCoordinator(hass, client, "machine/state", 10),
        DecaidCoordinator(hass, client, "workflow", 60),
        DecaidCoordinator(hass, client, "settings", 60),
        DecaidCoordinator(hass, client, "devices", 60),
    )
    await data.machine.async_config_entry_first_refresh()
    # Optional resources can recover after setup instead of blocking all entities.
    await asyncio.gather(*(c.async_refresh() for c in (data.workflow, data.settings, data.devices)))
    entry.runtime_data = data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Unload platforms and their coordinator listeners."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
