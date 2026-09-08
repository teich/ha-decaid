"""Shared entity identity."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class DecaidEntity(CoordinatorEntity):
    """Keep entity IDs stable when the tablet's IP changes."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Decent",
            manufacturer="Decent Espresso",
            model="Decaid",
            configuration_url=str(entry.runtime_data.client.base_url),
        )
