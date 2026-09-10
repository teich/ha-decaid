"""Connection status from Decaid's device inventory."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import CONF_MACHINE_ID
from .entity import DecaidEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            *(DecaidConnection(entry, kind) for kind in ("machine", "scale")),
            DecaidScaleLost(entry),
        ]
    )


class DecaidScaleLost(DecaidEntity, BinarySensorEntity):
    """Sequencer-reported scale loss, sticky for the remainder of the shot."""

    _attr_name = "Scale lost during shot"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry):
        super().__init__(entry.runtime_data.shot, entry, "scale_lost_during_shot")

    @property
    def is_on(self):
        return (self.coordinator.data or {}).get("scaleLost", False)


class DecaidConnection(DecaidEntity, BinarySensorEntity):
    """Report whether a matching device is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry, kind):
        super().__init__(entry.runtime_data.devices, entry, f"{kind}_connected")
        self._attr_name = f"{kind.capitalize()} connected"
        self.kind = kind
        self.data = entry.runtime_data
        self.machine_id = entry.data.get(CONF_MACHINE_ID)

    @property
    def is_on(self):
        if self.kind == "machine":
            return self.data.machine_connected(self.machine_id)
        return any(
            device.get("type") == "scale" and device.get("state") == "connected"
            for device in (self.coordinator.data or [])
        )
