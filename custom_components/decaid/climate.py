"""Bengle cup warmer."""

import math

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .entity import DecaidEntity, add_when_capable


async def async_setup_entry(hass, entry, async_add_entities):
    add_when_capable(entry, "cupWarmer", lambda: [DecaidCupWarmer(entry)], async_add_entities)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


# Home Assistant 2026.11 renamed the climate temperature API to native_*
# and deprecated the old names (removed in 2027.11). Provide whichever this
# Home Assistant expects; drop the old names once 2026.11 is the minimum.
NATIVE_TEMPERATURE_API = hasattr(ClimateEntity, "native_current_temperature")


class DecaidCupWarmer(DecaidEntity, ClimateEntity):
    """The cup warmer's manual heating and whole-degree setpoint."""

    _attr_name = "Cup warmer"
    _attr_icon = "mdi:coffee-outline"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1
    _attr_min_temp = 0
    _attr_max_temp = 80

    def __init__(self, entry):
        super().__init__(entry.runtime_data.cup_warmer, entry, "cup_warmer")

    @property
    def available(self):
        return super().available and isinstance(self.payload.get("enabled"), bool)

    @property
    def hvac_mode(self):
        return HVACMode.HEAT if self.payload.get("enabled") is True else HVACMode.OFF

    @property
    def hvac_action(self):
        if self.payload.get("enabled") is not True:
            return HVACAction.OFF
        current, target = self._current(), self._target()
        # Decaid does not report the heater's duty; infer it from the setpoint.
        if current is not None and target is not None and current >= target:
            return HVACAction.IDLE
        return HVACAction.HEATING

    def _current(self):
        return _number(self.payload.get("currentTemperature"))

    def _target(self):
        return _number(self.payload.get("temperature"))

    if NATIVE_TEMPERATURE_API:
        _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        native_current_temperature = property(_current)
        native_target_temperature = property(_target)
    else:  # Home Assistant before 2026.11
        _attr_temperature_unit = UnitOfTemperature.CELSIUS
        current_temperature = property(_current)
        target_temperature = property(_target)

    async def async_set_temperature(self, **kwargs):
        body = {}
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            body["temperature"] = int(round(temperature))
            # Decaid turns manual heating on with a temperature-only request.
            body["enabled"] = self.payload.get("enabled") is True
        if (mode := kwargs.get("hvac_mode")) is not None:
            body["enabled"] = mode == HVACMode.HEAT
        if body:
            await self.async_command("PUT", "machine/cupWarmer", body)

    async def async_set_hvac_mode(self, hvac_mode):
        await self.async_command(
            "PUT", "machine/cupWarmer", {"enabled": hvac_mode == HVACMode.HEAT}
        )

    async def async_turn_on(self):
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self):
        await self.async_set_hvac_mode(HVACMode.OFF)
