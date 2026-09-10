"""Asynchronous client for Decaid's local REST and WebSocket APIs."""

import asyncio
import math
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, ClientWSTimeout
from yarl import URL


class DecaidError(Exception):
    """A request failed or returned an invalid payload."""


def validate_payload(path: str, data: Any) -> dict | list:
    """Share validation between REST snapshots and streamed payloads."""
    expected = list if path == "devices" else dict
    if not isinstance(data, expected):
        raise DecaidError(f"Invalid response from {path}")
    if path == "devices" and any(not isinstance(item, dict) for item in data):
        raise DecaidError("Invalid device list")
    if path == "machine/state" and (
        not isinstance(data.get("state"), dict)
        or not isinstance(data["state"].get("state"), str)
        or not data["state"]["state"]
    ):
        raise DecaidError("Invalid machine state")
    if path == "machine/waterLevels" and any(
        isinstance(data.get(key), bool)
        or not isinstance(data.get(key), (int, float))
        or not math.isfinite(data[key])
        for key in ("currentLevel", "refillLevel")
    ):
        raise DecaidError("Invalid water levels")
    return data


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
        return validate_payload(path, data)

    async def connect_stream(self, path: str):
        """Open a socket; the stream owner handles ping/pong and cleanup."""
        url = self.base_url.with_scheme("ws") / "ws" / "v1" / path
        async with asyncio.timeout(10):
            return await self.session.ws_connect(
                url,
                autoping=False,
                timeout=ClientWSTimeout(ws_close=5),
                max_msg_size=1024 * 1024,
            )

    async def set_power(self, awake: bool) -> None:
        """Request wake or sleep; never start brewing."""
        await self.request("PUT", f"machine/state/{'idle' if awake else 'sleeping'}")
