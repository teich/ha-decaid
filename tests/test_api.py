"""HTTP boundary validation, including empty PUT responses."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectionError

from custom_components.decaid.api import DecaidClient, DecaidError, validate_payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"currentLevel": 28},
        {"currentLevel": True, "refillLevel": 5},
        {"currentLevel": "28", "refillLevel": 5},
        {"currentLevel": None, "refillLevel": 5},
        {"currentLevel": float("nan"), "refillLevel": 5},
        {"currentLevel": 28, "refillLevel": float("inf")},
    ],
)
def test_invalid_water_levels(payload):
    with pytest.raises(DecaidError, match="Invalid water levels"):
        validate_payload("machine/waterLevels", payload)


@pytest.mark.parametrize("payload", [[], None, "text", {"state": None}])
async def test_invalid_state(payload):
    client = DecaidClient(MagicMock(), "::1", 8080)
    client.request = AsyncMock(return_value=payload)
    with pytest.raises(DecaidError):
        await client.get("machine/state")


async def test_http_and_command():
    session = MagicMock()
    response = AsyncMock()
    response.status = 204
    session.request.return_value.__aenter__.return_value = response
    client = DecaidClient(session, "::1", 8080)
    await client.set_power(False)
    assert (
        str(session.request.call_args.args[1]) == "http://[::1]:8080/api/v1/machine/state/sleeping"
    )
    response.json.assert_not_called()
    response.status = 503
    with pytest.raises(DecaidError):
        await client.get("settings")
    session.request.return_value.__aenter__.side_effect = ClientConnectionError()
    with pytest.raises(DecaidError):
        await client.get("devices")


@pytest.mark.parametrize("error", [TimeoutError(), ValueError("invalid JSON")])
async def test_timeout_and_bad_json(error):
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json.side_effect = error
    session.request.return_value.__aenter__.return_value = response
    client = DecaidClient(session, "192.168.2.231", 8080)
    with pytest.raises(DecaidError):
        await client.get("settings")
