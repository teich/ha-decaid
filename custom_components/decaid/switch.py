"""Wake/sleep control; awake machines are never sent idle on turn-on."""

import asyncio
from collections.abc import Callable

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .api import DecaidError
from .const import CONF_MACHINE_ID
from .entity import DecaidEntity

POWER_TRANSITION_TIMEOUT = 20


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
        self._pending_power: bool | None = None
        self._cancel_transition: Callable[[], None] | None = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.data.devices.async_add_listener(self._handle_coordinator_update))
        self.async_on_remove(self._clear_transition)

    @callback
    def _clear_transition(self):
        """Drop the optimistic state and cancel its expiry timer."""
        self._pending_power = None
        if self._cancel_transition is not None:
            self._cancel_transition()
            self._cancel_transition = None

    @callback
    def _expire_transition(self, _now):
        """Return to the reported state even if polling data never changed."""
        self._clear_transition()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self):
        if self._pending_power is not None and (
            not self.available or (self.state_value != "sleeping") == self._pending_power
        ):
            self._clear_transition()
        # Commands also write optimistic states outside coordinator callbacks;
        # always reconcile them instead of using the read-only entity cache.
        self.async_write_ha_state()

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
        if self._pending_power is not None:
            return self._pending_power
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
                self._clear_transition()
                self.async_write_ha_state()
                return
            try:
                await self.data.client.set_power(awake)
            except DecaidError as err:
                self._clear_transition()
                self.async_write_ha_state()
                raise HomeAssistantError(f"Unable to change Decaid power: {err}") from err
            # The REST command can succeed before machine/state reflects it.
            # Hold the requested display state through stale polls, but never
            # indefinitely if the machine does not complete the transition.
            self._clear_transition()
            self._pending_power = awake
            self._cancel_transition = async_call_later(
                self.hass, POWER_TRANSITION_TIMEOUT, self._expire_transition
            )
            self.async_write_ha_state()
            await self.coordinator.async_refresh()
            # Equal coordinator data does not notify listeners. It may already
            # match a repeated command, so reconcile explicitly as well.
            self._handle_coordinator_update()

    async def async_turn_on(self, **kwargs):
        await self._async_power(True)

    async def async_turn_off(self, **kwargs):
        await self._async_power(False)
