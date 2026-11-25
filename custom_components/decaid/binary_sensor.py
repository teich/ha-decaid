"""Connection status from Decaid's device inventory."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .const import CONF_MACHINE_ID
from .entity import DecaidEntity, add_when_capable


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            *(DecaidConnection(entry, kind) for kind in ("machine", "scale")),
            DecaidScaleLost(entry),
            DecaidRefillKit(entry),
        ]
    )
    add_when_capable(entry, "preheat", lambda: [DecaidPreheatActive(entry)], async_add_entities)


class DecaidRefillKit(DecaidEntity, BinarySensorEntity):
    """Whether the machine detected a plumbed-in refill kit."""

    _attr_name = "Refill kit detected"
    _attr_icon = "mdi:water-pump"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry.runtime_data.machine_info, entry, "refill_kit")

    @property
    def available(self):
        return super().available and self.is_on is not None

    @property
    def is_on(self):
        extra = self.payload.get("extra")
        value = extra.get("refillKit") if isinstance(extra, dict) else None
        return value if isinstance(value, bool) else None


class DecaidPreheatActive(DecaidEntity, BinarySensorEntity):
    """Whether the Bengle cup warmer is pre-warming ahead of a scheduled wake."""

    _attr_name = "Cup warmer pre-warming"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, entry):
        super().__init__(entry.runtime_data.preheat, entry, "cup_warmer_preheat_active")

    @property
    def available(self):
        return super().available and isinstance(self.payload.get("active"), bool)

    @property
    def is_on(self):
        return self.payload.get("active") is True


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
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
