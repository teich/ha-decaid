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


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "unknown"},
        {"weight": True, "weightFlow": 0},
        {"weight": 0, "weightFlow": float("nan")},
        {"weight": 0, "weightFlow": 0, "battery": "50"},
        {"weight": 0, "weightFlow": 0, "timerValue": float("inf")},
    ],
)
def test_invalid_scale_payload(payload):
    with pytest.raises(DecaidError):
        validate_payload("scale/snapshot", payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"event": "bogus"},
        {"state": {}},
        {"timestamp": None},
        {"scaleLost": "false"},
        {"shotId": 123},
        {"decision": []},
        {"decision": {"kind": "stop", "reason": 123}},
    ],
)
def test_invalid_shot_payload(shot_payload, changes):
    with pytest.raises(DecaidError):
        validate_payload("machine/shotState", {**shot_payload, **changes})


def test_unknown_shot_decision_reason_is_supported(shot_payload):
    payload = {
        **shot_payload,
        "event": "decision",
        "decision": {"kind": "stop", "reason": "newFirmwareReason"},
    }
    assert validate_payload("machine/shotState", payload) == payload
