"""Constants for Decaid."""

from homeassistant.const import Platform

DOMAIN = "decaid"
DEFAULT_PORT = 8080
CONF_MACHINE_ID = "machine_id"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.EVENT]
