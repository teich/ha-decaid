"""Manual IP configuration; Decaid does not advertise discovery."""

from ipaddress import ip_address
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DecaidClient, DecaidError
from .const import CONF_MACHINE_ID, DEFAULT_PORT, DOMAIN


class DecaidConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up and reconfigure a tablet endpoint."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        return await self._async_form("user", user_input)

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        return await self._async_form("reconfigure", user_input)

    async def _async_form(self, step: str, user_input: dict[str, Any] | None):
        errors = {}
        entry = self._get_reconfigure_entry() if step == "reconfigure" else None
        defaults = user_input or (entry.data if entry else {})
        if user_input is not None:
            data = dict(user_input)
            try:
                data[CONF_HOST] = str(ip_address(data[CONF_HOST].strip()))
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            if not errors:
                for existing in self._async_current_entries():
                    if (not entry or existing.entry_id != entry.entry_id) and (
                        existing.data[CONF_HOST],
                        existing.data[CONF_PORT],
                    ) == (data[CONF_HOST], data[CONF_PORT]):
                        return self.async_abort(reason="already_configured")
                data[CONF_MACHINE_ID] = data.get(CONF_MACHINE_ID, "").strip()
                client = DecaidClient(
                    async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PORT]
                )
                try:
                    await client.get("machine/state")
                except DecaidError:
                    errors["base"] = "cannot_connect"
                else:
                    if entry:
                        return self.async_update_reload_and_abort(entry, data_updates=data)
                    return self.async_create_entry(title=f"Decent ({data[CONF_HOST]})", data=data)
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
