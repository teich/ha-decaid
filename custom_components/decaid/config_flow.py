"""Manual host configuration; Decaid does not advertise discovery."""

import asyncio
import re
from ipaddress import ip_address
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DecaidClient, DecaidError, machine_serial
from .const import CONF_MACHINE_ID, DEFAULT_PORT, DOMAIN

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOSTNAME = re.compile(rf"(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*")


def normalize_host(value: str) -> str | None:
    """Return a canonical IP address or hostname, or None if it is neither.

    URLs, ports, paths, and bracketed IPv6 literals are rejected.
    """
    host = value.strip()
    try:
        return str(ip_address(host))
    except ValueError:
        pass
    host = host.rstrip(".").lower()
    return host if _HOSTNAME.fullmatch(host) else None


def _title(entry, host: str) -> str:
    """Follow the host in the title, unless the user has renamed the entry."""
    if entry.title != f"Decent ({entry.data[CONF_HOST]})":
        return entry.title
    return f"Decent ({host})"


class DecaidConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up and reconfigure a tablet endpoint."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        return await self._async_form("user", user_input)

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        return await self._async_form("reconfigure", user_input)

    async def _async_validate(self, data: dict[str, Any]) -> dict[str, str]:
        """Check the tablet answers and that a given machine ID exists.

        Also note the connected machine's serial, if Decaid has one yet.
        """
        self._serial = None
        client = DecaidClient(async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PORT])
        state, devices, info = await asyncio.gather(
            client.get("machine/state"),
            client.get("devices"),
            client.get("machine/info"),
            return_exceptions=True,
        )
        for result in (state, devices, info):
            if isinstance(result, BaseException) and not isinstance(result, DecaidError):
                raise result
        # A machine that is off at the wall leaves machine/state failing while
        # the tablet itself is fine, so either endpoint proves reachability.
        if isinstance(state, DecaidError) and isinstance(devices, DecaidError):
            return {"base": "cannot_connect"}
        # machine/info describes whichever machine is connected. Trust its
        # serial only when that is the machine this entry selects.
        if isinstance(devices, list) and any(
            device.get("type") == "machine"
            and device.get("state") == "connected"
            and (not data[CONF_MACHINE_ID] or device.get("id") == data[CONF_MACHINE_ID])
            for device in devices
        ):
            self._serial = machine_serial(info)
        machine_id = data[CONF_MACHINE_ID]
        if (
            machine_id
            and isinstance(devices, list)
            and not any(
                device.get("type") == "machine" and device.get("id") == machine_id
                for device in devices
            )
        ):
            return {CONF_MACHINE_ID: "unknown_machine"}
        return {}

    async def _async_finish(self, entry, data: dict[str, Any]) -> ConfigFlowResult:
        serial = self._serial
        owner = (
            self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, serial)
            if serial
            else None
        )
        if entry:
            if owner is not None and owner.entry_id != entry.entry_id:
                return self.async_abort(reason="machine_already_configured")
            return self.async_update_reload_and_abort(
                entry,
                # The new tablet may be connected to a different machine. Without
                # its serial, clear the identity; the entry records it again
                # once the machine reports one.
                unique_id=serial,
                title=_title(entry, data[CONF_HOST]),
                data_updates=data,
            )
        if owner is not None:
            # A new tablet for a machine that is already set up, e.g. after
            # replacing the tablet: move the existing entry, keeping its
            # entities and history, rather than adding a duplicate.
            if all(owner.data.get(key) == value for key, value in data.items()):
                return self.async_abort(reason="already_configured")
            return self.async_update_reload_and_abort(
                owner,
                title=_title(owner, data[CONF_HOST]),
                data_updates=data,
                reason="moved_to_new_tablet",
            )
        if serial:
            await self.async_set_unique_id(serial)
        return self.async_create_entry(title=f"Decent ({data[CONF_HOST]})", data=data)

    async def _async_form(self, step: str, user_input: dict[str, Any] | None):
        errors = {}
        entry = self._get_reconfigure_entry() if step == "reconfigure" else None
        defaults = user_input or (entry.data if entry else {})
        if user_input is not None:
            data = dict(user_input)
            host = normalize_host(data[CONF_HOST])
            if host is None:
                errors[CONF_HOST] = "invalid_host"
            else:
                data[CONF_HOST] = host
                for existing in self._async_current_entries():
                    if (not entry or existing.entry_id != entry.entry_id) and (
                        existing.data[CONF_HOST],
                        existing.data[CONF_PORT],
                    ) == (data[CONF_HOST], data[CONF_PORT]):
                        return self.async_abort(reason="already_configured")
                data[CONF_MACHINE_ID] = data.get(CONF_MACHINE_ID, "").strip()
                errors = await self._async_validate(data)
                if not errors:
                    return await self._async_finish(entry, data)
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                    vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                    vol.Optional(CONF_MACHINE_ID, default=defaults.get(CONF_MACHINE_ID, "")): str,
                }
            ),
            errors=errors,
        )
