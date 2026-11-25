"""Decaid integration for Decent Espresso machines."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .api import DecaidClient, known_value, machine_serial
from .const import CONF_MACHINE_ID, DOMAIN, PLATFORMS
from .coordinator import (
    DecaidCoordinator,
    DecaidData,
    DecaidLastShotCoordinator,
    DecaidPushCoordinator,
    DecaidScaleCoordinator,
    DecaidShotCoordinator,
    DecaidShotSettingsCoordinator,
    DecaidWaterCoordinator,
)

type DecaidConfigEntry = ConfigEntry[DecaidData]


async def async_setup_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Set up polling and platforms."""
    client = DecaidClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )

    def poll(path, seconds):
        return DecaidCoordinator(hass, client, path, seconds, config_entry=entry)

    def push(path, seconds, stream_path):
        return DecaidPushCoordinator(hass, client, path, seconds, stream_path, config_entry=entry)

    data = DecaidData(
        client,
        push("machine/state", 10, "machine/snapshot"),
        poll("workflow", 60),
        poll("settings", 60),
        push("devices", 60, "devices"),
        DecaidWaterCoordinator(hass, client, config_entry=entry),
        DecaidScaleCoordinator(hass, client, config_entry=entry),
        DecaidShotCoordinator(hass, client, config_entry=entry),
        DecaidShotSettingsCoordinator(hass, client, config_entry=entry),
        DecaidLastShotCoordinator(hass, client, config_entry=entry),
        poll("machine/info", 300),
        poll("machine/capabilities", 300),
        # Each read is several BLE register reads; writes refresh it at once.
        poll("machine/settings", 300),
        poll("display", 60),
        poll("presence/settings", 60),
        # Streamed so install progress shows live; REST covers a dropped socket.
        push("update", 900, "update"),
        poll("machine/firmware", 3600),
        # Bengle only: these poll only once their entities have been added.
        poll("machine/cupWarmer", 30),
        poll("machine/cupWarmer/preheat", 60),
    )
    # The tablet is reachable if either the machine state or the device
    # inventory answers. A machine that is off at the wall leaves machine/state
    # failing, but the tablet's own entities should still load.
    results = await asyncio.gather(
        data.machine.async_config_entry_first_refresh(),
        data.devices.async_config_entry_first_refresh(),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, ConfigEntryNotReady):
            raise result
    if all(isinstance(result, ConfigEntryNotReady) for result in results):
        raise results[0]
    # Optional resources can recover after setup instead of blocking all entities.
    await asyncio.gather(
        *(
            c.async_refresh()
            for c in (
                data.workflow,
                data.settings,
                data.water,
                data.shots,
                data.machine_info,
                data.capabilities,
                data.machine_settings,
                data.display,
                data.presence,
                data.app_update,
                data.firmware,
            )
        )
    )
    entry.runtime_data = data
    machine_was_connected = None

    @callback
    def devices_updated():
        nonlocal machine_was_connected
        if data.devices.last_update_success:
            connected = data.machine_connected(entry.data.get(CONF_MACHINE_ID))
            data.machine.set_device_connected(connected)
            data.water.set_device_connected(connected)
            data.shot_settings.set_device_connected(connected)
            if connected and machine_was_connected is False:
                # Machine-backed resources fail while it is away; reread them
                # now rather than waiting out their long poll intervals.
                for coordinator in (
                    data.machine_info,
                    data.capabilities,
                    data.machine_settings,
                    data.firmware,
                ):
                    entry.async_create_background_task(
                        hass, coordinator.async_request_refresh(), "Decaid machine reconnect"
                    )
            machine_was_connected = connected
            if connected:
                record_machine_serial(hass, entry, data)

    entry.async_on_unload(data.devices.async_add_listener(devices_updated))
    devices_updated()

    @callback
    def machine_info_updated():
        update_device_info(hass, entry, data.machine_info.data)
        record_machine_serial(hass, entry, data)

    entry.async_on_unload(data.machine_info.async_add_listener(machine_info_updated))

    cancel_shot_refresh = None

    @callback
    def refresh_last_shot(_now):
        nonlocal cancel_shot_refresh
        cancel_shot_refresh = None
        entry.async_create_background_task(
            hass, data.shots.async_request_refresh(), "Decaid last shot"
        )

    @callback
    def shot_updated():
        nonlocal cancel_shot_refresh
        shot = data.shot.data or {}
        if shot.get("event") == "terminal" and not data.shot.is_replay:
            # Decaid saves the shot as it finishes; give the write a moment.
            if cancel_shot_refresh is not None:
                cancel_shot_refresh()
            cancel_shot_refresh = async_call_later(hass, 5, refresh_last_shot)

    @callback
    def cancel_pending_shot_refresh():
        if cancel_shot_refresh is not None:
            cancel_shot_refresh()

    entry.async_on_unload(data.shot.async_add_listener(shot_updated))
    entry.async_on_unload(cancel_pending_shot_refresh)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    update_device_info(hass, entry, data.machine_info.data)
    record_machine_serial(hass, entry, data)
    for coordinator in data.streams:
        coordinator.start(entry)
    return True


@callback
def record_machine_serial(hass: HomeAssistant, entry: DecaidConfigEntry, data) -> None:
    """Identify the entry by its machine, so a new tablet can find it.

    Entries made while the machine was away, or before this was added, get
    the serial the first time the machine reports it. Once set it is kept:
    Reconfigure is the way to move an entry to a different machine.
    """
    if entry.unique_id is not None:
        return
    # machine/info describes whichever machine is connected; it only
    # identifies this entry when that is the machine the entry selects.
    if not data.machine_connected(entry.data.get(CONF_MACHINE_ID)):
        return
    serial = machine_serial(data.machine_info.data)
    if serial is None:
        return
    issue_id = f"duplicate_machine_{entry.entry_id}"
    owner = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, serial)
    if owner is None:
        hass.config_entries.async_update_entry(entry, unique_id=serial)
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    # This machine already has an entry, e.g. a replacement tablet added
    # before the machine reported its serial. Merging would delete one of
    # the user's entries, so ask them to resolve it instead.
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="duplicate_machine",
        translation_placeholders={"entry": entry.title, "existing": owner.title},
    )


@callback
def update_device_info(hass: HomeAssistant, entry: DecaidConfigEntry, info) -> None:
    """Show the connected machine's model, serial number, and firmware."""
    registry = dr.async_get(hass)
    identifier = (DOMAIN, entry.entry_id)
    if hasattr(registry, "async_get_device_by_identifier"):
        device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    else:  # Home Assistant before 2026.11
        device = registry.async_get_device(identifiers={identifier})
    if device is None:
        return
    if not isinstance(info, dict):
        if device.model is None:
            registry.async_update_device(device.id, model="Decaid")
        return
    model = known_value(info.get("model")) or (None if device.model else "Decaid")
    changes = {
        key: value
        for key, value in (
            ("model", model),
            ("serial_number", machine_serial(info)),
            ("sw_version", known_value(info.get("version"))),
        )
        if value is not None and getattr(device, key) != value
    }
    if changes:
        registry.async_update_device(device.id, **changes)


async def async_remove_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> None:
    """Drop any duplicate-machine notice raised for this entry."""
    ir.async_delete_issue(hass, DOMAIN, f"duplicate_machine_{entry.entry_id}")


async def async_unload_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Unload platforms and their coordinator listeners."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await asyncio.gather(*(coordinator.stop() for coordinator in entry.runtime_data.streams))
    return True
