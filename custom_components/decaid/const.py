"""Constants for Decaid."""

from homeassistant.const import Platform

DOMAIN = "decaid"
DEFAULT_PORT = 8080
CONF_MACHINE_ID = "machine_id"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]
