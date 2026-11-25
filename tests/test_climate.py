"""The cup warmer picks the climate temperature API Home Assistant provides.

CI runs a Home Assistant without the native_* climate API (before 2026.11),
so the native branch is exercised by reimporting the module against a
ClimateEntity that has it.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components import climate as ha_climate
from homeassistant.components.climate import HVACAction
from homeassistant.const import UnitOfTemperature

from custom_components.decaid import climate

LEGACY = ("current_temperature", "target_temperature", "_attr_temperature_unit")
NATIVE = (
    "native_current_temperature",
    "native_target_temperature",
    "_attr_native_temperature_unit",
)


class NativeClimateEntity(ha_climate.ClimateEntity):
    """Stands in for Home Assistant 2026.11+'s ClimateEntity."""

    @property
    def native_current_temperature(self):
        return None


HOST_NATIVE = hasattr(ha_climate.ClimateEntity, "native_current_temperature")


def _load(native):
    if native == HOST_NATIVE:
        return importlib.reload(climate)
    # Only reached on a host without the native API: give it one.
    with patch.object(ha_climate, "ClimateEntity", NativeClimateEntity):
        return importlib.reload(climate)


@pytest.fixture(autouse=True)
def restore_module():
    yield
    importlib.reload(climate)


def _warmer(module, payload):
    entry = MagicMock()
    entry.runtime_data.client.base_url = "http://tablet:8080"
    entry.runtime_data.cup_warmer.data = payload
    return module.DecaidCupWarmer(entry)


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_cup_warmer_temperature_api(native):
    if HOST_NATIVE and not native:
        # A subclass cannot hide the native API from hasattr, and the legacy
        # branch only exists for Home Assistant before 2026.11.
        pytest.skip("legacy climate API needs Home Assistant before 2026.11")
    module = _load(native)
    cls = module.DecaidCupWarmer
    assert module.NATIVE_TEMPERATURE_API is native
    provided, other = (NATIVE, LEGACY) if native else (LEGACY, NATIVE)
    # Only the names this Home Assistant expects; the others would log a
    # deprecation warning on 2026.11+ or be ignored before it.
    assert all(name in vars(cls) for name in provided)
    assert not any(name in vars(cls) for name in other)

    heating = _warmer(module, {"temperature": 55, "enabled": True, "currentTemperature": 48.5})
    assert getattr(heating, provided[2]) == UnitOfTemperature.CELSIUS
    assert getattr(heating, provided[0]) == 48.5
    assert getattr(heating, provided[1]) == 55
    assert heating.hvac_action == HVACAction.HEATING
    holding = _warmer(module, {"temperature": 55, "enabled": True, "currentTemperature": 55.0})
    assert holding.hvac_action == HVACAction.IDLE
    off = _warmer(module, {"temperature": 55, "enabled": False, "currentTemperature": 30})
    assert off.hvac_action == HVACAction.OFF
    unknown = _warmer(module, {"temperature": 55, "enabled": True, "currentTemperature": None})
    assert getattr(unknown, provided[0]) is None
    assert unknown.hvac_action == HVACAction.HEATING
