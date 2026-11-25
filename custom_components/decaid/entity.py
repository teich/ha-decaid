"""Shared entity identity, commands, and capability-gated setup."""

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DecaidError
from .const import DOMAIN


class DecaidEntity(CoordinatorEntity):
    """Keep entity IDs stable when the tablet's IP changes."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # Model, serial number, and firmware come from machine/info; see
        # update_device_info. Leaving them out here keeps a late entity from
        # resetting them.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Decent",
            manufacturer="Decent Espresso",
            configuration_url=str(entry.runtime_data.client.base_url),
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._last_coordinator_state = self._coordinator_state()

    def _coordinator_state(self):
        """What Home Assistant would record for this entity.

        Attributes count too: a climate entity's temperatures and an update
        entity's progress change while the state itself stays the same.
        """
        if not self.available:
            return (False, None, None, None)
        return (True, self.state, self.state_attributes, self.extra_state_attributes)

    @callback
    def _handle_coordinator_update(self):
        """Write only when this entity's value, attributes, or availability change."""
        state = self._coordinator_state()
        if state != self._last_coordinator_state:
            self._last_coordinator_state = state
            self.async_write_ha_state()

    @property
    def payload(self) -> dict:
        data = self.coordinator.data
        return data if isinstance(data, dict) else {}

    async def async_command(self, method: str, path: str, body: Any = None, refresh=True):
        """Send a command, reporting failures as Home Assistant errors."""
        client = self.platform.config_entry.runtime_data.client
        try:
            await client.command(method, path, body)
        except DecaidError as err:
            raise HomeAssistantError(f"Decaid command failed: {err}") from err
        if refresh:
            await self.coordinator.async_request_refresh()


def add_when_capable(
    entry,
    capability: str,
    factory: Callable[[], Iterable[Entity]],
    async_add_entities,
) -> None:
    """Add entities once the connected machine reports a capability.

    The machine may be away when Home Assistant starts, so capabilities can
    arrive later. Entities are added once and kept, so a Bengle that drops off
    the network shows them as unavailable rather than removing them.
    """
    data = entry.runtime_data
    added = False

    @callback
    def check():
        nonlocal added
        if not added and data.has_capability(capability):
            added = True
            entities = list(factory())
            async_add_entities(entities)
            # Their coordinators have not been read yet; do it now rather than
            # waiting a full poll interval.
            for coordinator in {id(e.coordinator): e.coordinator for e in entities}.values():
                entry.async_create_background_task(
                    coordinator.hass, coordinator.async_request_refresh(), "Decaid capability"
                )

    check()
    if not added:
        entry.async_on_unload(data.capabilities.async_add_listener(check))
