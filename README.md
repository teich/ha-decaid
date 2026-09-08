# Decaid for Home Assistant

Bring your Decent Espresso machine into Home Assistant through [Decaid](https://github.com/decentespresso/decaid)'s local REST API.

Plain REST YAML works for readings and wake/sleep control, but it doesn't group those entities into a Home Assistant device. This integration puts them together under one **Decent** device, with setup through the UI and installation through HACS.

![Decent device in Home Assistant, with power control and machine sensors](docs/device.png)

## What you get

- Wake/sleep switch that only sends a wake command when the machine is sleeping.
- State, substate, temperatures, pressure, flow, and their targets, polled every 10 seconds.
- Profile, dose/yield targets, tablet battery, and machine/scale connectivity, polled every 60 seconds.

After a successful wake/sleep command, the switch shows the requested state while the machine catches up, for up to 20 seconds. Polling then confirms it or restores the reported state.

REST only for now. The sleep command can interrupt a running operation.

## Install

Requires Home Assistant 2025.2+ and a tablet running Decaid with its REST API reachable from Home Assistant.

1. In **HACS → Custom repositories**, add `https://github.com/teich/ha-decaid` as an **Integration**.
2. Download **Decaid — Decent Espresso** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Decaid**.
4. Enter your tablet's IP address and port (default `8080`). There's no automatic discovery yet. Leave the optional machine device ID blank unless you need to match a specific machine.

Use **Reconfigure** if the tablet's IP changes; your entity IDs stay the same.

Moving from REST YAML? Remove your old Decent REST sensors, commands, and template switch first. Existing entities aren't migrated automatically, so check the entity IDs used by your dashboards and automations.

## AI disclaimer

This integration was written by AI. It's an unofficial community project, with no affiliation to Decent Espresso and no warranty. Review it and use it at your own risk.

[Report an issue](https://github.com/teich/ha-decaid/issues) · [Decaid API reference](https://github.com/decentespresso/decaid/blob/main/doc/Api.md)
