"""Sequencer events for Home Assistant automations."""

from homeassistant.components.event import EventEntity
from homeassistant.core import callback

from .entity import DecaidEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DecaidShotEvent(entry)])


class DecaidShotEvent(DecaidEntity, EventEntity):
    """Preserve each live event without replaying events on reconnect."""

    _attr_name = "Shot event"
    _attr_icon = "mdi:coffee-maker"
    _attr_event_types = ["state", "decision", "terminal"]

    def __init__(self, entry):
        super().__init__(entry.runtime_data.shot, entry, "shot_event")

    @callback
    def _handle_coordinator_update(self):
        if self.available and not self.coordinator.is_replay:
            data = self.coordinator.data
            self._trigger_event(
                data["event"],
                {
                    "shot_id": data.get("shotId"),
                    "phase": data["state"],
                    "source_timestamp": data["timestamp"],
                    "scale_lost": data["scaleLost"],
                    "decision": data.get("decision"),
                },
            )
        self.async_write_ha_state()
