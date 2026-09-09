"""Enable loading the local integration in Home Assistant."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def mock_stream_start():
    """Keep legacy REST tests offline; transport tests drive sockets explicitly."""
    from custom_components.decaid.stream import DecaidStream

    real_start = DecaidStream.start
    with patch("custom_components.decaid.stream.DecaidStream.start", autospec=True) as start:
        start.real_start = real_start
        yield start
