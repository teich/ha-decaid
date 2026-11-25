"""Asynchronous client for Decaid's local REST and WebSocket APIs."""

import asyncio
import math
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, ClientWSTimeout
from yarl import URL


class DecaidError(Exception):
    """A request failed or returned an invalid payload."""


def _is_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_payload(path: str, data: Any) -> dict | list:
    """Share validation between REST snapshots and streamed payloads."""
    route = path.partition("?")[0]
    expected = list if route == "devices" else dict
    if not isinstance(data, expected):
        raise DecaidError(f"Invalid response from {path}")
    if route == "shots" and (
        not isinstance(data.get("total"), int) or isinstance(data["total"], bool)
    ):
        raise DecaidError("Invalid shot list")
    if path == "devices" and any(not isinstance(item, dict) for item in data):
        raise DecaidError("Invalid device list")
    if path == "machine/state" and (
        not isinstance(data.get("state"), dict)
        or not isinstance(data["state"].get("state"), str)
        or not data["state"]["state"]
    ):
        raise DecaidError("Invalid machine state")
    if path == "machine/waterLevels" and any(
        not _is_number(data.get(key)) for key in ("currentLevel", "refillLevel")
    ):
        raise DecaidError("Invalid water levels")
    if path == "scale/snapshot":
        if "status" in data:
            if data["status"] not in ("connected", "disconnected"):
                raise DecaidError("Invalid scale status")
        elif any(not _is_number(data.get(key)) for key in ("weight", "weightFlow")) or any(
            data.get(key) is not None and not _is_number(data[key])
            for key in ("battery", "timerValue")
        ):
            raise DecaidError("Invalid scale snapshot")
    if path == "machine/shotSettings" and any(
        not _is_number(data.get(key))
        for key in (
            "targetSteamTemp",
            "targetSteamDuration",
            "targetHotWaterTemp",
            "targetHotWaterVolume",
            "targetHotWaterDuration",
            "targetShotVolume",
            "groupTemp",
        )
    ):
        raise DecaidError("Invalid shot settings")
    if path == "machine/shotState":
        if (
            data.get("event") not in ("state", "decision", "terminal")
            or any(
                not isinstance(data.get(key), str) or not data[key]
                for key in ("state", "timestamp")
            )
            or any(
                not isinstance(data.get(key), bool)
                for key in ("scaleConnected", "scaleLost", "machineHasAutonomousSAW")
            )
            or (data.get("shotId") is not None and not isinstance(data["shotId"], str))
        ):
            raise DecaidError("Invalid shot event")
        decision = data.get("decision")
        if decision is not None and (
            not isinstance(decision, dict)
            or not isinstance(decision.get("kind"), str)
            or not isinstance(decision.get("reason"), str)
        ):
            raise DecaidError("Invalid shot decision")
    return data


def known_value(value) -> str | None:
    """Decaid reports placeholders before the machine's handshake completes."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and value not in ("", "0", "Unknown") else None


def machine_serial(info: Any) -> str | None:
    """The connected machine's serial number, once Decaid has read it.

    Older DE1s report 0 until Decaid resolves their serial from the owner's
    Decent account, so it can be missing even while the machine is connected.
    """
    return known_value(info.get("serialNumber")) if isinstance(info, dict) else None


class DecaidClient:
    """Use Home Assistant's shared HTTP session."""

    def __init__(self, session: ClientSession, host: str, port: int) -> None:
        self.session = session
        self.base_url = URL.build(scheme="http", host=host, port=port)

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        """Read JSON, or accept an empty successful command response."""
        # A "?query" is split off: yarl would percent-encode it inside a path segment.
        route, _, query = path.partition("?")
        url = self.base_url / "api" / "v1" / route
        if query:
            url = url.with_query(query)
        try:
            async with self.session.request(
                method,
                url,
                json=body,
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

    async def command(self, method: str, path: str, body: Any = None) -> None:
        """Send a settings or accessory command; never one that starts brewing."""
        await self.request(method, path, body)

    async def set_power(self, awake: bool) -> None:
        """Request wake or sleep; never start brewing."""
        await self.request("PUT", f"machine/state/{'idle' if awake else 'sleeping'}")
