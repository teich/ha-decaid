"""Small asynchronous client for Decaid's local REST API."""

import asyncio
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL


class DecaidError(Exception):
    """A request failed or returned an invalid payload."""


class DecaidClient:
    """Use Home Assistant's shared HTTP session."""

    def __init__(self, session: ClientSession, host: str, port: int) -> None:
        self.session = session
        self.base_url = URL.build(scheme="http", host=host, port=port)

    async def request(self, method: str, path: str) -> Any:
        """Read JSON, or accept an empty successful command response."""
        try:
            async with self.session.request(
                method,
                self.base_url / "api" / "v1" / path,
                timeout=ClientTimeout(total=5 if method == "GET" else 10),
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise DecaidError(f"{path}: HTTP {response.status}")
                if method != "GET":
                    return None
                return await response.json(content_type=None)
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise DecaidError(f"Unable to read {path}: {err}") from err

    async def get(self, path: str) -> dict | list:
        """Validate the endpoint's top-level response shape."""
        data = await self.request("GET", path)
        expected = list if path == "devices" else dict
        if not isinstance(data, expected):
            raise DecaidError(f"Invalid response from {path}")
        if path == "devices" and any(not isinstance(item, dict) for item in data):
            raise DecaidError("Invalid device list")
        if path == "machine/state" and not isinstance(data.get("state"), dict):
            raise DecaidError("Invalid machine state")
        return data

    async def set_power(self, awake: bool) -> None:
        """Request wake or sleep; never start brewing."""
        await self.request("PUT", f"machine/state/{'idle' if awake else 'sleeping'}")
