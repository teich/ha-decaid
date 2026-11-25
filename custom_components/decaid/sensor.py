"""Machine measurements, workflow targets, and tablet battery."""

import math
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import DecaidEntity


@dataclass(frozen=True, kw_only=True)
class DecaidSensorDescription(SensorEntityDescription):
    """Map an entity to a nested JSON field."""

    source: str = "machine"
    path: tuple[str, ...]
    numeric: bool = False
    # Decimal places kept in the state. Finer jitter in live readings would
    # otherwise write a new recorder row on almost every update.
    precision: int | None = None


SENSORS = [
    DecaidSensorDescription(
        key="scale_weight",
        name="Scale weight",
        source="scale",
        path=("weight",),
        numeric=True,
        precision=1,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    DecaidSensorDescription(
        key="scale_weight_flow",
        name="Scale weight flow",
        source="scale",
        path=("weightFlow",),
        numeric=True,
        precision=1,
        native_unit_of_measurement="g/s",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water",
    ),
    DecaidSensorDescription(
        key="scale_battery",
        name="Scale battery",
        source="scale",
        path=("battery",),
        numeric=True,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    DecaidSensorDescription(
        key="scale_timer",
        name="Scale timer",
        source="scale",
        path=("timerValue",),
        numeric=True,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
    ),
    DecaidSensorDescription(
        key="shot_phase",
        name="Shot phase",
        source="shot",
        path=("state",),
        icon="mdi:coffee-maker",
    ),
    DecaidSensorDescription(
        key="last_shot_stop_reason",
        name="Last shot stop reason",
        source="shot",
        path=("stopReason",),
        icon="mdi:information-outline",
    ),
    DecaidSensorDescription(
        key="state", name="State", path=("state", "state"), icon="mdi:coffee-maker"
    ),
    DecaidSensorDescription(key="substate", name="Substate", path=("state", "substate")),
    DecaidSensorDescription(
        key="water_level",
        name="Water level",
        source="water",
        path=("currentLevel",),
        numeric=True,
        precision=1,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:cup-water",
    ),
    *[
        DecaidSensorDescription(
            key=key,
            name=name,
            path=(field,),
            numeric=True,
            precision=1,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for key, name, field in (
            ("group_temperature", "Grouphead temperature", "groupTemperature"),
            ("mix_temperature", "Mix temperature", "mixTemperature"),
            ("steam_temperature", "Steam temperature", "steamTemperature"),
            ("target_group_temperature", "Target grouphead temperature", "targetGroupTemperature"),
            ("target_mix_temperature", "Target mix temperature", "targetMixTemperature"),
        )
    ],
    *[
        DecaidSensorDescription(
            key=key,
            name=name,
            path=(field,),
            numeric=True,
            precision=1,
            native_unit_of_measurement=UnitOfPressure.BAR,
            device_class=SensorDeviceClass.PRESSURE,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for key, name, field in (
            ("pressure", "Pressure", "pressure"),
            ("target_pressure", "Target pressure", "targetPressure"),
        )
    ],
    *[
        DecaidSensorDescription(
            key=key,
            name=name,
            path=(field,),
            numeric=True,
            precision=1,
            native_unit_of_measurement=UnitOfVolumeFlowRate.MILLILITERS_PER_SECOND,
            device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water",
        )
        for key, name, field in (
            ("flow", "Flow", "flow"),
            ("target_flow", "Target flow", "targetFlow"),
        )
    ],
    DecaidSensorDescription(
        key="profile",
        name="Profile",
        source="workflow",
        path=("profile", "title"),
        icon="mdi:chart-bell-curve",
    ),
    *[
        DecaidSensorDescription(
            key=key,
            name=name,
            source="workflow",
            path=("context", field),
            numeric=True,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            device_class=SensorDeviceClass.WEIGHT,
        )
        for key, name, field in (
            ("target_dose", "Target dose", "targetDoseWeight"),
            ("target_yield", "Target yield", "targetYield"),
        )
    ],
    DecaidSensorDescription(
        key="tablet_battery",
        name="Tablet battery",
        source="settings",
        path=("chargingState", "batteryPercent"),
        numeric=True,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


# Sensors that became writable number entities.
MOVED_TO_NUMBER = (
    "target_steam_temperature",
    "target_hot_water_temperature",
    "configured_group_temperature",
    "target_steam_duration",
    "target_hot_water_duration",
    "target_hot_water_volume",
    "target_shot_volume",
    "refill_threshold",
)

# Sensors that were dropped: Decaid's shot and steam history only covers what
# the tablet recorded, not the machine's own lifetime counts.
REMOVED = ("shot_count", "steam_count")


async def async_setup_entry(hass, entry, async_add_entities):
    registry = er.async_get(hass)
    for key in (*MOVED_TO_NUMBER, *REMOVED):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id is not None:
            registry.async_remove(entity_id)
    async_add_entities(
        [
            *(
                (DecaidStopReasonSensor if d.key == "last_shot_stop_reason" else DecaidSensor)(
                    getattr(entry.runtime_data, d.source), entry, d
                )
                for d in SENSORS
            ),
            DecaidLastShotSensor(entry),
        ]
    )


class DecaidSensor(DecaidEntity, SensorEntity):
    """A nullable reading from a REST resource."""

    entity_description: DecaidSensorDescription

    def __init__(self, coordinator, entry, description):
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        value = self.coordinator.data
        for key in self.entity_description.path:
            value = value.get(key) if isinstance(value, dict) else None
        if self.entity_description.numeric:
            if isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (ValueError, TypeError, OverflowError):
                return None
            if not math.isfinite(number):
                return None
            if self.entity_description.precision is not None:
                return round(number, self.entity_description.precision)
            return number
        return value if isinstance(value, str) and value else None


class DecaidStopReasonSensor(DecaidSensor, RestoreEntity):
    """Keep the last stop reason across restarts until a new one arrives."""

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self.coordinator.restore_stop_reason(last.state)


def _value(mapping, key):
    value = mapping.get(key) if isinstance(mapping, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return value if isinstance(value, str) and value else None


class DecaidLastShotSensor(DecaidEntity, SensorEntity):
    """When the newest shot in Decaid's history was pulled, with its details."""

    _attr_name = "Last shot"
    _attr_icon = "mdi:coffee"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry):
        super().__init__(entry.runtime_data.shots, entry, "last_shot")

    @property
    def _shot(self):
        latest = self.payload.get("latest")
        return latest if isinstance(latest, dict) else {}

    @property
    def native_value(self):
        timestamp = dt_util.parse_datetime(self._shot.get("timestamp") or "")
        if timestamp is None:
            return None
        if timestamp.tzinfo is None:
            # Decaid writes the tablet's local time without an offset.
            timestamp = timestamp.replace(tzinfo=dt_util.get_default_time_zone())
        return timestamp

    @property
    def extra_state_attributes(self):
        shot = self._shot
        if not shot:
            return None
        workflow = shot.get("workflow") if isinstance(shot.get("workflow"), dict) else {}
        profile = workflow.get("profile") if isinstance(workflow.get("profile"), dict) else {}
        context = workflow.get("context") if isinstance(workflow.get("context"), dict) else {}
        notes = shot.get("annotations") if isinstance(shot.get("annotations"), dict) else {}
        summary = self.payload.get("summary") or {}
        dose = _value(notes, "actualDoseWeight") or _value(context, "targetDoseWeight")
        beverage = (
            _value(notes, "actualYield")
            or _value(summary, "final_weight")
            or _value(context, "targetYield")
        )
        attributes = {
            "shot_id": shot.get("id"),
            "profile": _value(profile, "title"),
            "dose": dose,
            "yield": beverage,
            "ratio": round(beverage / dose, 2)
            if isinstance(dose, (int, float)) and isinstance(beverage, (int, float)) and dose
            else None,
            "target_dose": _value(context, "targetDoseWeight"),
            "target_yield": _value(context, "targetYield"),
            "duration": _value(summary, "duration"),
            "peak_pressure": _value(summary, "peak_pressure"),
            "peak_flow": _value(summary, "peak_flow"),
            "final_weight": _value(summary, "final_weight"),
            "stop_reason": _value(shot, "stopReason"),
            "coffee_name": _value(context, "coffeeName"),
            "coffee_roaster": _value(context, "coffeeRoaster"),
            "grinder": _value(context, "grinderModel"),
            "grinder_setting": _value(context, "grinderSetting"),
            "barista": _value(context, "baristaName"),
            "drinker": _value(context, "drinkerName"),
            "enjoyment": _value(notes, "enjoyment"),
            "tds": _value(notes, "drinkTds"),
            "extraction_yield": _value(notes, "drinkEy"),
            "notes": _value(notes, "espressoNotes"),
        }
        return {key: value for key, value in attributes.items() if value is not None}
