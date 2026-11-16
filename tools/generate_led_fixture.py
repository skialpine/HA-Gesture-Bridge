"""Actual HA select/number/service boundary used by LED tests and C4 fixture export."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import logging
from pathlib import Path
import shutil
import tempfile

from homeassistant import config_entries, loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components.event import EventEntity, DATA_COMPONENT as EVENTS
from homeassistant.components.select import SelectEntity
from homeassistant.components.select.const import DATA_COMPONENT as SELECTS
from homeassistant.components.number import NumberEntity
from homeassistant.components.number.const import DATA_COMPONENT as NUMBERS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.setup import async_setup_component

from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent


class FixtureSelect(SelectEntity):
    _attr_should_poll = False

    def __init__(self, name, options, current, calls, feedback=True):
        self._attr_name, self._attr_unique_id = name, name.lower().replace(' ', '_')
        self._attr_options, self._attr_current_option = options, current
        self.calls, self.feedback, self.gate = calls, feedback, None

    async def async_select_option(self, option):
        self.calls.append((self.entity_id, option))
        if self.gate is not None:
            await self.gate.wait()
        if self.feedback:
            self._attr_current_option = option
            self.async_write_ha_state()


class FixtureNumber(NumberEntity):
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 2
    _attr_native_step = 1
    _attr_native_value = 0
    _attr_name = "Fixture LED Brightness"
    _attr_unique_id = "fixture_led_brightness"

    def __init__(self, calls, feedback=True):
        self.calls, self.feedback = calls, feedback

    async def async_set_native_value(self, value):
        self.calls.append((self.entity_id, value))
        if self.feedback:
            self._attr_native_value = value
            self.async_write_ha_state()


class FixturePaddle(EventEntity):
    _attr_name = "Fixture LED Paddle"
    _attr_unique_id = "fixture_led_paddle"
    _attr_event_types = ["KeyPressed", "KeyHeldDown", "KeyReleased"]


async def setup_fixture(hass, *, feedback=True, configure=None):
    """Return actual entities plus hardware-boundary call records; no network."""
    for domain in (DOMAIN, "event", "select", "number"):
        assert await async_setup_component(hass, domain, {})
    calls = []
    color = FixtureSelect("Fixture LED Color", ["White", "Blue", "Red", "Green"], "White", calls, feedback)
    power = FixtureSelect("Fixture LED Mode", ["Always on", "Always off", "Load dependent"], "Always on", calls, feedback)
    brightness = FixtureNumber(calls, feedback)
    paddle = FixturePaddle()
    await hass.data[SELECTS].async_add_entities([color, power])
    await hass.data[NUMBERS].async_add_entities([brightness])
    await hass.data[EVENTS].async_add_entities([paddle])
    config = {"unique_id": "fixture-led-control", "name": "Fixture LED Control",
        "source_entity_id": paddle.entity_id, "actions": {
            "KeyPressed": {"kind": "press", "press_count": 1},
            "KeyHeldDown": {"kind": "hold_start"}, "KeyReleased": {"kind": "release"}},
        "leds": [{"id": "status", "label": "Status LED", "button_id": "toggle",
            "color": {"entity_id": color.entity_id, "values": {"FFFFFF": "White", "0000FF": "Blue", "FF0000": "Red", "00FF00": "Green"}},
            "power": {"entity_id": power.entity_id, "on": "Always on", "off": "Always off"},
            "brightness": {"entity_id": brightness.entity_id, "values": {"100": 0, "60": 1, "30": 2}}}]}
    if configure is not None:
        configure(config)
    aggregate = GestureBridgeEvent(NativeControlConfig.parse(config))
    aggregate._session_id = "fixture-led-session"
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="event",
        platform_name=DOMAIN, platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    await platform.async_add_entities([aggregate])
    await hass.async_block_till_done()
    return {"aggregate": aggregate, "color": color, "power": power, "brightness": brightness,
            "paddle": paddle, "calls": calls, "config": deepcopy(config)}


def state_fixture(hass, aggregate):
    state = hass.states.get(aggregate.entity_id)
    return {"entity_id": state.entity_id, "state": state.state,
            "last_updated": "2026-11-15T09:00:00.000Z", "attributes": dict(state.attributes)}


async def export():
    with tempfile.TemporaryDirectory(prefix="ha-led-fixture-") as folder:
        shutil.copytree(Path(__file__).resolve().parents[1] / "custom_components/ha_gesture_bridge",
                        Path(folder) / "custom_components/ha_gesture_bridge", ignore=shutil.ignore_patterns("__pycache__"))
        hass = HomeAssistant(folder)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        loader.async_setup(hass)
        assert await async_load_base_functionality(hass)
        try:
            fixture = await setup_fixture(hass)
            return state_fixture(hass, fixture["aggregate"])
        finally:
            await hass.async_stop(force=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(asyncio.run(export()), indent=2, sort_keys=True) + '\n')
    print(f"Exported actual HA LED state to {args.output}")


if __name__ == "__main__":
    main()
