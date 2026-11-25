"""Scale and presence buttons. None of these start an operation on the machine."""

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .entity import DecaidEntity


@dataclass(frozen=True, kw_only=True)
class DecaidButtonDescription(ButtonEntityDescription):
    """A command button and the resource that decides its availability."""

    source: str
    path: str
    method: str = "PUT"


BUTTONS = (
    DecaidButtonDescription(
        key="scale_tare", name="Tare scale", source="devices", path="scale/tare", icon="mdi:scale"
    ),
    DecaidButtonDescription(
        key="scale_timer_start",
        name="Start scale timer",
        source="devices",
        path="scale/timer/start",
        icon="mdi:timer-play-outline",
    ),
    DecaidButtonDescription(
        key="scale_timer_stop",
        name="Stop scale timer",
        source="devices",
        path="scale/timer/stop",
        icon="mdi:timer-stop-outline",
    ),
    DecaidButtonDescription(
        key="scale_timer_reset",
        name="Reset scale timer",
        source="devices",
        path="scale/timer/reset",
        icon="mdi:timer-refresh-outline",
    ),
    # Restarts Decaid's idle-sleep countdown; it does not wake a sleeping machine.
    DecaidButtonDescription(
        key="presence_heartbeat",
        name="Signal presence",
        source="presence",
        path="machine/heartbeat",
        method="POST",
        icon="mdi:account-check",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(DecaidButton(entry, description) for description in BUTTONS)


class DecaidButton(DecaidEntity, ButtonEntity):
    """Send one command."""

    entity_description: DecaidButtonDescription

    def __init__(self, entry, description):
        super().__init__(getattr(entry.runtime_data, description.source), entry, description.key)
        self.entity_description = description

    @property
    def available(self):
        if not super().available:
            return False
        if self.entity_description.source == "devices":
            return any(
                device.get("type") == "scale" and device.get("state") == "connected"
                for device in (self.coordinator.data or [])
                if isinstance(device, dict)
            )
        # Decaid ignores heartbeats while presence detection is off.
        return self.payload.get("userPresenceEnabled") is True

    async def async_press(self) -> None:
        await self.async_command(
            self.entity_description.method, self.entity_description.path, refresh=False
        )
