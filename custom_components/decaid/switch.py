"""Wake/sleep control; awake machines are never sent idle on turn-on."""

import asyncio

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .api import DecaidError
from .const import CONF_MACHINE_ID
from .entity import DecaidEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DecaidPower(entry)])


class DecaidPower(DecaidEntity, SwitchEntity):
    """Power means awake, not mains power."""

    _attr_name = "Power"
    _attr_icon = "mdi:coffee-maker"

    def __init__(self, entry):
        super().__init__(entry.runtime_data.machine, entry, "power")
        self.data = entry.runtime_data
        self.machine_id = entry.data.get(CONF_MACHINE_ID)
        self._command_lock = asyncio.Lock()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.data.devices.async_add_listener(self._handle_coordinator_update))

    @property
    def state_value(self):
        state = (self.coordinator.data or {}).get("state")
        return state.get("state") if isinstance(state, dict) else None

    @property
    def available(self):
        return (
            super().available
            and self.data.machine_connected(self.machine_id)
            and isinstance(self.state_value, str)
            and self.state_value not in ("", "unknown", "disconnected")
        )

    @property
    def is_on(self):
        return self.state_value != "sleeping"

    async def _async_power(self, awake: bool):
        async with self._command_lock:
            # Refresh before deciding whether an idle request is needed.
            await asyncio.gather(
                self.coordinator.async_refresh(), self.data.devices.async_refresh()
            )
            if not self.available:
                raise HomeAssistantError("Decaid machine is unavailable")
            if awake and self.state_value != "sleeping":
                return
            try:
                await self.data.client.set_power(awake)
            except DecaidError as err:
                raise HomeAssistantError(f"Unable to change Decaid power: {err}") from err
            await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs):
        await self._async_power(True)

    async def async_turn_off(self, **kwargs):
        await self._async_power(False)
