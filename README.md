# Decaid for Home Assistant

A local REST integration for Decent Espresso machines running **Decaid** on the tablet. Includes manual IPv4/IPv6 setup, a configurable port (default `8080`), and a wake/sleep switch. No cloud account or discovery is needed.

## Installation

### HACS

1. In HACS, open **Custom repositories**, add `https://github.com/teich/ha-decaid`, and select **Integration**.
2. Download **Decaid — Decent Espresso** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Decaid**.
4. Enter your tablet's IP address, for example `192.168.2.231`, and port `8080`.

The optional machine device ID can normally be left blank. To reproduce an exact device match, enter its ID from `/api/v1/devices` (for example the Bluetooth address from your existing YAML). Scale connectivity means any connected device with type `scale`.

### Manual

Copy `custom_components/decaid` into your Home Assistant `config/custom_components` directory, restart, and add **Decaid** as above.

Home Assistant 2025.2 or newer is required. Automated tests currently run on Home Assistant 2026.9.1 / Python 3.14. Local integration artwork requires Home Assistant 2026.3 or newer.

## Entities

| Resource | Interval | Entities |
| --- | --- | --- |
| `/api/v1/machine/state` | 10 seconds | State, substate, grouphead/mix/steam temperatures, target grouphead/mix temperatures, pressure/target pressure, flow/target flow |
| `/api/v1/workflow` | 60 seconds | Profile, target dose, target yield |
| `/api/v1/settings` | 60 seconds | Tablet battery |
| `/api/v1/devices` | 60 seconds | Machine connected, scale connected |
| Machine state + devices | On polling updates | Power switch |

The power switch sends `PUT /api/v1/machine/state/idle` only when the freshly read state is `sleeping`. Turning it off sends `PUT /api/v1/machine/state/sleeping`. This is wake/sleep control, not mains power. Off can interrupt a running operation, matching the original YAML behavior. REST reads and writes cannot provide an atomic guard against state changes made concurrently on the tablet.

GET requests time out after 5 seconds and commands after 10 seconds. Failed polls make the affected resource's entities unavailable and recover automatically; missing or invalid individual readings are unknown. The switch also requires successful device polling and a connected machine. No brew-start, firmware, settings-write, or WebSocket operations are exposed.

Use the integration's **Reconfigure** menu if the IP, port, or machine ID changes. Entity unique IDs use the config entry identity and survive reconfiguration. Different tablet endpoints can be configured separately.

## Moving from YAML

Remove the old Decent `rest`, `rest_command`, and template switch entries and restart before adding this integration. Do not remove unrelated REST integrations. Existing YAML unique IDs are not adopted automatically. Check entity IDs in your dashboards and automations; if old entities remain registered, remove those old entities and rename the new ones as needed. With no name conflicts, the default IDs include `sensor.decent_state` and `switch.decent_power`.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use the Home Assistant test harness with mocked REST responses. They do not contact or control a physical machine.

## Releases and support

Report integration issues at [teich/ha-decaid](https://github.com/teich/ha-decaid/issues). For releases, update the version in `custom_components/decaid/manifest.json` and publish a matching GitHub tag. HACS can also install directly from the default branch.

## References

- [Decaid API reference](https://github.com/decentespresso/decaid/blob/main/doc/Api.md)
- [Decaid REST schema](https://github.com/decentespresso/decaid/blob/main/assets/api/rest_v1.yml)
- [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/)
