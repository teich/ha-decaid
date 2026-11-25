"""Machine setting choices."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from .entity import DecaidEntity

STEAM_PURGE_MODES = {0: "normal", 1: "two_tap_stop"}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DecaidSteamPurgeMode(entry)])


class DecaidSteamPurgeMode(DecaidEntity, SelectEntity):
    """How the machine ends steaming: purge at once, or wait for a second tap."""

    _attr_translation_key = "steam_purge_mode"
    _attr_icon = "mdi:kettle-steam-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(STEAM_PURGE_MODES.values())

    def __init__(self, entry):
        super().__init__(entry.runtime_data.machine_settings, entry, "steam_purge_mode")

    @property
    def available(self):
        return super().available and self.current_option is not None

    @property
    def current_option(self):
        mode = self.payload.get("steamPurgeMode")
        return STEAM_PURGE_MODES.get(mode) if isinstance(mode, int) else None

    async def async_select_option(self, option: str) -> None:
        mode = next(key for key, value in STEAM_PURGE_MODES.items() if value == option)
        await self.async_command("POST", "machine/settings", {"steamPurgeMode": mode})
