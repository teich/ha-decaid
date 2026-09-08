"""Machine measurements, workflow targets, and tablet battery."""

import math
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfPressure, UnitOfTemperature

from .entity import DecaidEntity


@dataclass(frozen=True, kw_only=True)
class DecaidSensorDescription(SensorEntityDescription):
    """Map an entity to a nested JSON field."""

    source: str = "machine"
    path: tuple[str, ...]
    numeric: bool = False


SENSORS = [
    DecaidSensorDescription(
        key="state", name="State", path=("state", "state"), icon="mdi:coffee-maker"
    ),
    DecaidSensorDescription(key="substate", name="Substate", path=("state", "substate")),
    *[
        DecaidSensorDescription(
            key=key,
            name=name,
            path=(field,),
            numeric=True,
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
            native_unit_of_measurement="mL/s",
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
    ),
]


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        DecaidSensor(getattr(entry.runtime_data, d.source), entry, d) for d in SENSORS
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
            return number if math.isfinite(number) else None
        return value if isinstance(value, str) and value else None
