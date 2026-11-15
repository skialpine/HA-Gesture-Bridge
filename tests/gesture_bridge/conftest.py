"""Small fixtures using the real HA core at the project's exact baseline."""

from pathlib import Path
import shutil

from homeassistant import config_entries, loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.core import HomeAssistant
import pytest


@pytest.fixture
async def hass(tmp_path):
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "custom_components/ha_gesture_bridge",
        tmp_path / "custom_components/ha_gesture_bridge",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    loader.async_setup(instance)
    assert await async_load_base_functionality(instance)
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)
