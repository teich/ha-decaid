"""Writable settings. None of these start an operation on the machine."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.exceptions import HomeAssistantError

from .entity import DecaidEntity, add_when_capable

SHOT_SETTINGS_FIELDS = (
    "targetSteamTemp",
    "targetSteamDuration",
    "targetHotWaterTemp",
    "targetHotWaterVolume",
    "targetHotWaterDuration",
    "targetShotVolume",
    "groupTemp",
)


def _shot_settings_body(entity: "DecaidNumber", value: float) -> dict:
    """The endpoint replaces every shot setting, so resend the current ones."""
    current = entity.payload
    if any(not isinstance(current.get(key), (int, float)) for key in SHOT_SETTINGS_FIELDS):
        raise HomeAssistantError("Decaid shot settings are not available yet")
    body = {key: current[key] for key in SHOT_SETTINGS_FIELDS}
    body["steamSetting"] = current.get("steamSetting", 0)
    body[entity.entity_description.field] = value
    return body


@dataclass(frozen=True, kw_only=True)
class DecaidNumberDescription(NumberEntityDescription):
    """Map a number to a JSON field and the request that writes it."""

    source: str
    field: str
    method: str = "POST"
    path: str
    integer: bool = True
    body: Callable[["DecaidNumber", float], Any] = field(
        default=lambda entity, value: {entity.entity_description.field: value}
    )
    # Stream-backed values come back over their socket; polled ones need a refresh.
    refresh: bool = True
    supported_key: str | None = None


def _shot_setting(key, name, field_name, unit, device_class, low, high, step=1, integer=True):
    return DecaidNumberDescription(
        key=key,
        name=name,
        source="shot_settings",
        field=field_name,
        path="machine/shotSettings",
        body=_shot_settings_body,
        refresh=False,
        integer=integer,
        native_unit_of_measurement=unit,
        device_class=device_class,
        native_min_value=low,
        native_max_value=high,
        native_step=step,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )


def _machine_setting(key, name, field_name, unit, device_class, low, high, step, integer=False):
    return DecaidNumberDescription(
        key=key,
        name=name,
        source="machine_settings",
        field=field_name,
        path="machine/settings",
        integer=integer,
        native_unit_of_measurement=unit,
        device_class=device_class,
        native_min_value=low,
        native_max_value=high,
        native_step=step,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )


NUMBERS = (
    # Decaid treats steam temperatures below 135 °C as steam off, and writes 0.
    _shot_setting(
        "target_steam_temperature",
        "Target steam temperature",
        "targetSteamTemp",
        UnitOfTemperature.CELSIUS,
        NumberDeviceClass.TEMPERATURE,
        0,
        170,
    ),
    _shot_setting(
        "target_steam_duration",
        "Target steam duration",
        "targetSteamDuration",
        UnitOfTime.SECONDS,
        NumberDeviceClass.DURATION,
        0,
        255,
    ),
    _shot_setting(
        "target_hot_water_temperature",
        "Target hot water temperature",
        "targetHotWaterTemp",
        UnitOfTemperature.CELSIUS,
        NumberDeviceClass.TEMPERATURE,
        0,
        100,
    ),
    _shot_setting(
        "target_hot_water_volume",
        "Target hot water volume",
        "targetHotWaterVolume",
        UnitOfVolume.MILLILITERS,
        NumberDeviceClass.VOLUME,
        0,
        255,
    ),
    _shot_setting(
        "target_hot_water_duration",
        "Target hot water duration",
        "targetHotWaterDuration",
        UnitOfTime.SECONDS,
        NumberDeviceClass.DURATION,
        0,
        255,
    ),
    _shot_setting(
        "target_shot_volume",
        "Target shot volume",
        "targetShotVolume",
        UnitOfVolume.MILLILITERS,
        NumberDeviceClass.VOLUME,
        0,
        255,
    ),
    _shot_setting(
        "configured_group_temperature",
        "Configured group temperature",
        "groupTemp",
        UnitOfTemperature.CELSIUS,
        NumberDeviceClass.TEMPERATURE,
        0,
        110,
        step=0.1,
        integer=False,
    ),
    DecaidNumberDescription(
        key="refill_threshold",
        name="Refill threshold",
        source="water",
        field="refillLevel",
        path="machine/waterLevels",
        refresh=False,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        device_class=NumberDeviceClass.DISTANCE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        icon="mdi:water-alert",
        entity_category=EntityCategory.CONFIG,
    ),
    _machine_setting(
        "fan_threshold",
        "Fan threshold",
        "fan",
        UnitOfTemperature.CELSIUS,
        NumberDeviceClass.TEMPERATURE,
        0,
        100,
        1,
        integer=True,
    ),
    _machine_setting(
        "tank_temperature_threshold",
        "Tank temperature threshold",
        "tankTemp",
        UnitOfTemperature.CELSIUS,
        NumberDeviceClass.TEMPERATURE,
        0,
        100,
        1,
        integer=True,
    ),
    _machine_setting(
        "flush_temperature",
        "Flush temperature",
        "flushTemp",
        UnitOfTemperature.CELSIUS,
        NumberDeviceClass.TEMPERATURE,
        0,
        100,
        0.1,
    ),
    _machine_setting(
        "flush_flow",
        "Flush flow",
        "flushFlow",
        UnitOfVolumeFlowRate.MILLILITERS_PER_SECOND,
        NumberDeviceClass.VOLUME_FLOW_RATE,
        0,
        10,
        0.1,
    ),
    _machine_setting(
        "flush_timeout",
        "Flush timeout",
        "flushTimeout",
        UnitOfTime.SECONDS,
        NumberDeviceClass.DURATION,
        0,
        300,
        0.1,
    ),
    _machine_setting(
        "hot_water_flow",
        "Hot water flow",
        "hotWaterFlow",
        UnitOfVolumeFlowRate.MILLILITERS_PER_SECOND,
        NumberDeviceClass.VOLUME_FLOW_RATE,
        0,
        10,
        0.1,
    ),
    _machine_setting(
        "steam_flow",
        "Steam flow",
        "steamFlow",
        UnitOfVolumeFlowRate.MILLILITERS_PER_SECOND,
        NumberDeviceClass.VOLUME_FLOW_RATE,
        0,
        10,
        0.01,
    ),
    DecaidNumberDescription(
        key="display_brightness",
        name="Tablet screen brightness",
        source="display",
        field="brightness",
        method="PUT",
        path="display/brightness",
        supported_key="brightness",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        icon="mdi:brightness-6",
        entity_category=EntityCategory.CONFIG,
    ),
    DecaidNumberDescription(
        key="sleep_timeout",
        name="Sleep timeout",
        source="presence",
        field="sleepTimeoutMinutes",
        path="presence/settings",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=NumberDeviceClass.DURATION,
        native_min_value=0,
        native_max_value=240,
        native_step=1,
        mode=NumberMode.BOX,
        icon="mdi:sleep",
        entity_category=EntityCategory.CONFIG,
    ),
)

PREHEAT_LEAD = DecaidNumberDescription(
    key="cup_warmer_preheat_lead",
    name="Cup warmer pre-warm lead time",
    source="preheat",
    field="leadMinutes",
    method="PUT",
    path="machine/cupWarmer/preheat",
    native_unit_of_measurement=UnitOfTime.MINUTES,
    device_class=NumberDeviceClass.DURATION,
    native_min_value=0,
    native_max_value=120,
    native_step=1,
    mode=NumberMode.BOX,
    icon="mdi:timer-cog-outline",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(DecaidNumber(entry, description) for description in NUMBERS)
    add_when_capable(
        entry, "preheat", lambda: [DecaidNumber(entry, PREHEAT_LEAD)], async_add_entities
    )


class DecaidNumber(DecaidEntity, NumberEntity):
    """A setting read from one resource and written back to Decaid."""

    entity_description: DecaidNumberDescription

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
        return super().available and self.native_value is not None

    @property
    def native_value(self):
        value = self.payload.get(self.entity_description.field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if math.isfinite(value) else None

    async def async_set_native_value(self, value: float) -> None:
        description = self.entity_description
        value = int(round(value)) if description.integer else round(value, 2)
        stream = getattr(self.coordinator, "stream", None)
        await self.async_command(
            description.method,
            description.path,
            description.body(self, value),
            # A stream-backed value arrives over its socket; while the socket
            # is down, read it back over REST instead.
            refresh=description.refresh or (stream is not None and not stream.active),
        )
