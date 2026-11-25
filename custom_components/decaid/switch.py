"""Wake/sleep control and setting switches.

Awake machines are never sent idle on turn-on, and busy machines are never
sent to sleep on turn-off.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .api import DecaidError
from .const import CONF_MACHINE_ID
from .entity import DecaidEntity, add_when_capable

POWER_TRANSITION_TIMEOUT = 20

# Decaid MachineState values for operations that a sleep request would abort.
BUSY_STATES = frozenset(
    {
        "espresso",
        "hotWater",
        "flush",
        "steam",
        "steamRinse",
        "skipStep",
        "cleaning",
        "descaling",
        "calibration",
        "selfTest",
        "airPurge",
        "fwUpgrade",
    }
)


@dataclass(frozen=True, kw_only=True)
class DecaidSwitchDescription(SwitchEntityDescription):
    """A boolean setting and the requests that change it."""

    source: str
    field: str
    turn_on: tuple[str, str, Any]
    turn_off: tuple[str, str, Any]
    supported_key: str | None = None


SWITCHES = (
    DecaidSwitchDescription(
        key="usb_charger",
        name="USB charger",
        source="machine_settings",
        field="usb",
        turn_on=("POST", "machine/settings", {"usb": "enable"}),
        turn_off=("POST", "machine/settings", {"usb": "disable"}),
        icon="mdi:usb-port",
        entity_category=EntityCategory.CONFIG,
    ),
    DecaidSwitchDescription(
        key="screen_keep_awake",
        name="Tablet screen keep awake",
        source="display",
        field="wakeLockOverride",
        turn_on=("POST", "display/wakelock", None),
        turn_off=("DELETE", "display/wakelock", None),
        supported_key="wakeLock",
        icon="mdi:cellphone-screenshot",
    ),
    DecaidSwitchDescription(
        key="presence_detection",
        name="Presence detection",
        source="presence",
        field="userPresenceEnabled",
        turn_on=("POST", "presence/settings", {"userPresenceEnabled": True}),
        turn_off=("POST", "presence/settings", {"userPresenceEnabled": False}),
        icon="mdi:account-clock",
        entity_category=EntityCategory.CONFIG,
    ),
)

PREHEAT = DecaidSwitchDescription(
    key="cup_warmer_preheat",
    name="Cup warmer pre-warm",
    source="preheat",
    field="enabled",
    turn_on=("PUT", "machine/cupWarmer/preheat", {"enabled": True}),
    turn_off=("PUT", "machine/cupWarmer/preheat", {"enabled": False}),
    icon="mdi:calendar-clock",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DecaidPower(entry), *(DecaidSettingSwitch(entry, d) for d in SWITCHES)])
    add_when_capable(
        entry, "preheat", lambda: [DecaidSettingSwitch(entry, PREHEAT)], async_add_entities
    )


class DecaidSettingSwitch(DecaidEntity, SwitchEntity):
    """A setting switch; none of these start an operation on the machine."""

    entity_description: DecaidSwitchDescription

    def __init__(self, entry, description):
        super().__init__(getattr(entry.runtime_data, description.source), entry, description.key)
        self.entity_description = description

    @property
    def available(self):
        supported = self.entity_description.supported_key
        if supported is not None:
            platform = self.payload.get("platformSupported")
            if isinstance(platform, dict) and platform.get(supported) is False:
                return False
        return super().available and self.is_on is not None

    @property
    def is_on(self):
        value = self.payload.get(self.entity_description.field)
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs):
        await self.async_command(*self.entity_description.turn_on)

    async def async_turn_off(self, **kwargs):
        await self.async_command(*self.entity_description.turn_off)


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
        # Without a device inventory, a valid machine state read is enough:
        # Decaid cannot report one for a machine that is not connected.
        machine_present = (
            self.data.machine_connected(self.machine_id)
            if self.data.devices.last_update_success
            else True
        )
        return (
            super().available
            and machine_present
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
            if awake != (self.state_value == "sleeping"):
                # Already in the requested state: never send idle to an awake
                # machine (it stops an active shot) or a redundant sleep.
                self._clear_transition()
                self.async_write_ha_state()
                return
            if not awake and self.state_value in BUSY_STATES:
                raise HomeAssistantError(
                    f"Decaid machine is busy ({self.state_value}); "
                    "not sending it to sleep, which would abort the operation"
                )
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
