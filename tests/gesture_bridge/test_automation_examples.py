"""Execute the HA-only example with the real automation and EventEntity APIs."""
import json
import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.components.event import EventEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import yaml

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent


class Paddle(EventEntity):
    _attr_name = "Automation Paddle"
    _attr_event_types = ["double", "triple"]

    async def press(self, action):
        self._trigger_event(action)
        self.async_write_ha_state()
        await self.hass.async_block_till_done()


async def test_double_tap_example_accepts_repeats_but_not_snapshots_or_restore(hass):
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    source = Paddle()
    await component.async_add_entities([source])
    hass.states.async_set("switch.office", "off")
    config = NativeControlConfig.parse({"unique_id": "office_scene_control", "name": "Office Scene Control",
        "source_entity_id": source.entity_id, "load_entity_id": "switch.office",
        "actions": {"double": {"kind": "press", "press_count": 2},
                    "triple": {"kind": "press", "press_count": 3}}})
    entity = GestureBridgeEvent(config)
    await component.async_add_entities([entity])
    await hass.async_block_till_done()
    assert entity.entity_id == "event.office_scene_control"
    example = yaml.safe_load((Path(__file__).resolve().parents[2] / "examples/ha-only-double-tap.yaml").read_text())
    calls = []
    async def record(call):
        calls.append(call.data)
    hass.services.async_register("gesture_example_test", "record", record)
    example["automation"][0]["actions"] = [{"action": "gesture_example_test.record"}]
    await hass.async_start()
    assert await async_setup_component(hass, "automation", example)
    await hass.async_block_till_done()
    await source.press("double")
    assert len(calls) == 1
    await source.press("double")
    assert len(calls) == 2
    hass.states.async_set("switch.office", "on")
    await hass.async_block_till_done()
    assert len(calls) == 2
    await source.press("triple")
    assert len(calls) == 2
    before = hass.states.get(entity.entity_id)
    preserved = json.loads(json.dumps(before.attributes))
    preserved["bridge_session_id"] = "new-session"
    preserved["bridge_update_kind"] = "snapshot"
    hass.states.async_set(entity.entity_id, before.state, preserved)
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_deviceside_example_only_sends_start_and_reported_stop(hass, monkeypatch):
    instant = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: instant[0])

    class HoldPaddle(EventEntity):
        _attr_event_types = ["held", "release"]
        def __init__(self, name):
            self._attr_name = name
        async def press(self, action):
            instant[0] += timedelta(milliseconds=100)
            self._trigger_event(action)
            self.async_write_ha_state()
            await self.hass.async_block_till_done()
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    top, bottom = HoldPaddle("Top Source"), HoldPaddle("Bottom Source")
    await component.async_add_entities([top, bottom])
    config = NativeControlConfig.parse({"unique_id": "office_scene_control", "name": "Office Scene Control",
        "load_entity_id": "light.office", "buttons": [
            {"id": bid, "label": bid.title(), "source_entity_id": source.entity_id,
             "actions": {"held": {"kind": "hold_start"}, "release": {"kind": "release"}}}
            for bid, source in (("top", top), ("bottom", bottom))]})
    entity = GestureBridgeEvent(config)
    await component.async_add_entities([entity])
    example = yaml.safe_load((Path(__file__).resolve().parents[2] / "examples/deviceside-hold.yaml").read_text())
    calls = []
    async def record(call):
        calls.append((call.service, call.data))
    for service in ("raise_brightness", "lower_brightness", "stop"):
        hass.services.async_register("deviceside", service, record)
    await hass.async_start()
    assert await async_setup_component(hass, "automation", example)
    await hass.async_block_till_done()
    await top.press("held")
    assert [name for name, _ in calls] == ["raise_brightness"]
    await top.press("held")
    assert [name for name, _ in calls] == ["raise_brightness"]  # hold repeat
    hass.states.async_set("light.office", "on", {"brightness": 128, "supported_color_modes": ["brightness"]})
    await hass.async_block_till_done()
    assert [name for name, _ in calls] == ["raise_brightness"]  # metadata only
    await top.press("release")
    await bottom.press("held")
    await bottom.press("release")
    assert [name for name, _ in calls] == ["raise_brightness", "stop", "lower_brightness", "stop"]
    assert all(call[1]["entity_id"] == ["light.office"] for call in calls)
