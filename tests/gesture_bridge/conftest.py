"""Small fixtures using the real HA core at the project's exact baseline."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

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


@pytest.fixture
def run_consumer():
    """Run optional real C4 consumers without blocking HA's event loop."""
    checkout = os.environ.get("C4_HA_SWITCH_CHECKOUT")
    if not checkout:
        pytest.skip("Set C4_HA_SWITCH_CHECKOUT to run optional C4 consumer checks")
    root = Path(checkout).resolve()
    if not (root / "module/gestures.lua").is_file():
        pytest.fail("C4_HA_SWITCH_CHECKOUT does not contain the gesture consumer")

    async def run(executable, entrypoint, fixture, *args):
        result = await asyncio.to_thread(
            subprocess.run,
            [executable, str(entrypoint), str(fixture), *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    return run
