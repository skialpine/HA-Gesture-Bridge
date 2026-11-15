from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import shutil
import subprocess
import os

from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.components.event import EventEntity
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
import voluptuous as vol

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent, PLATFORM_SCHEMA, async_setup_platform
from tools.validate_gesture_contract import validate_state

SOURCE = "event.fixture_paddle"
LOAD = "switch.fixture_load"


def consumer_root():
    checkout = os.environ.get("C4_HA_SWITCH_CHECKOUT")
    if not checkout:
        pytest.skip("Set C4_HA_SWITCH_CHECKOUT to run optional C4 consumer checks")
    root = Path(checkout).resolve()
    if not (root / "module/gestures.lua").is_file():
        pytest.fail("C4_HA_SWITCH_CHECKOUT does not contain the gesture consumer")
    return root

CONFIG = {
    "platform": "ha_gesture_bridge",
    "name": "Fixture Control",
    "unique_id": "fixture-control",
    "source_entity_id": SOURCE,
    "button_id": "top",
    "button_label": "Top",
    "load_entity_id": LOAD,
    "actions": {
        "KeyPressed2x": {"kind": "press", "press_count": 2, "label": "Double Press"},
        "KeyPressed3x": {"kind": "press", "press_count": 3, "label": "Triple Press"},
    },
}


def wire_state(hass, entity):
    state = hass.states.get(entity.entity_id)
    result = json.loads(json.dumps(state.as_dict(), default=lambda value: value.isoformat()))
    validate_state(result)
    return result


async def add_bridge(hass, config=None):
    entities = []
    await async_setup_platform(hass, PLATFORM_SCHEMA(config or deepcopy(CONFIG)), entities.extend)
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities(entities)
    await hass.async_block_till_done()
    assert entities[0].entity_id is not None
    return component, entities[0]


async def source_press(hass, action="KeyPressed2x"):
    hass.states.async_set(SOURCE, dt_util.utcnow().isoformat(), {"event_type": action})
    await hass.async_block_till_done()


@pytest.fixture
def ready_sources(hass):
    hass.states.async_set(SOURCE, "unknown", {"event_type": None})
    hass.states.async_set(LOAD, "off")


async def test_first_press_from_unknown_and_repeated_identical_presses(hass, ready_sources):
    _, entity = await add_bridge(hass)
    initial = wire_state(hass, entity)
    assert initial["state"] == "unknown"
    assert initial["attributes"]["bridge_update_kind"] == "snapshot"
    assert "gesture_sequence" not in initial["attributes"]
    await source_press(hass)
    first = wire_state(hass, entity)
    assert first["attributes"]["gesture_id"] == "press_2"
    assert first["attributes"]["gesture_sequence"] == 1
    assert first["attributes"]["bridge_update_kind"] == "gesture"
    await source_press(hass)
    second = wire_state(hass, entity)
    assert second["attributes"]["gesture_sequence"] == 2
    assert second["attributes"]["gesture_event_id"] != first["attributes"]["gesture_event_id"]
    assert second["state"] > first["state"]


async def test_component_loads_through_real_ha_platform_setup(hass, ready_sources):
    assert await async_setup_component(hass, "event", {"event": [deepcopy(CONFIG)]})
    await hass.async_block_till_done()
    initial = hass.states.get("event.fixture_control")
    assert initial is not None
    assert initial.attributes["bridge_control_id"] == CONFIG["unique_id"]
    await source_press(hass)
    state = hass.states.get("event.fixture_control")
    assert state.attributes["gesture_id"] == "press_2"
    validate_state(json.loads(json.dumps(state.as_dict(), default=lambda value: value.isoformat())))


async def test_real_native_event_precision_preserves_first_press_same_millisecond(hass, monkeypatch):
    instant = datetime(2026, 11, 14, 20, 0, 0, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: instant)

    class NativePaddle(EventEntity):
        _attr_name = "Fixture Paddle"
        _attr_event_types = ["KeyPressed2x"]

    source = NativePaddle()
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([source])
    assert source.entity_id == SOURCE
    _, aggregate = await add_bridge(hass)
    instant += timedelta(microseconds=333)
    source._trigger_event("KeyPressed2x")
    source.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(SOURCE).state.endswith(".123+00:00")
    assert wire_state(hass, aggregate)["attributes"]["gesture_sequence"] == 1
    source._trigger_event("KeyPressed2x")
    source.async_write_ha_state()
    await hass.async_block_till_done()
    assert wire_state(hass, aggregate)["attributes"]["gesture_sequence"] == 2


async def test_baseline_restored_native_source_without_emitting(hass):
    old = dt_util.utcnow() - timedelta(hours=1)
    hass.states.async_set(SOURCE, old.isoformat(), {"event_type": "KeyPressed3x"})
    _, entity = await add_bridge(hass)
    initial = wire_state(hass, entity)
    assert initial["state"] == "unknown"
    assert initial["attributes"]["event_type"] is None
    await source_press(hass)
    assert wire_state(hass, entity)["attributes"]["gesture_sequence"] == 1


async def test_native_attribute_only_update_is_not_a_press(hass, ready_sources):
    _, entity = await add_bridge(hass)
    await source_press(hass)
    first = wire_state(hass, entity)
    source = hass.states.get(SOURCE)
    hass.states.async_set(SOURCE, source.state, {"event_type": "KeyPressed3x", "other": "metadata"})
    await hass.async_block_till_done()
    result = wire_state(hass, entity)
    assert result["state"] == first["state"]
    assert result["attributes"]["gesture_event_id"] == first["attributes"]["gesture_event_id"]
    assert result["attributes"]["bridge_update_kind"] == "snapshot"


async def test_linked_load_changes_preserve_last_actual_gesture(hass, ready_sources):
    _, entity = await add_bridge(hass)
    await source_press(hass)
    first = wire_state(hass, entity)
    hass.states.async_set(LOAD, "on")
    await hass.async_block_till_done()
    changed = wire_state(hass, entity)
    assert changed["attributes"]["bridge_load"]["state"] == "on"
    assert changed["attributes"]["bridge_update_kind"] == "snapshot"
    assert changed["state"] == first["state"]
    assert changed["attributes"]["gesture_event_id"] == first["attributes"]["gesture_event_id"]
    hass.states.async_set(LOAD, "unavailable")
    await hass.async_block_till_done()
    unavailable = wire_state(hass, entity)
    assert unavailable["attributes"]["bridge_load"]["available"] is False
    assert unavailable["state"] == first["state"]
    await source_press(hass)
    assert wire_state(hass, entity)["attributes"]["gesture_sequence"] == 2


async def test_evententity_restore_preserves_old_session_as_snapshot(hass, ready_sources):
    component, old_entity = await add_bridge(hass)
    await source_press(hass, "KeyPressed3x")
    original = wire_state(hass, old_entity)
    identity = old_entity.entity_id
    await old_entity.async_remove()
    await hass.async_block_till_done()
    new_entity = GestureBridgeEvent(NativeControlConfig.parse(CONFIG))
    await component.async_add_entities([new_entity])
    await hass.async_block_till_done()
    assert new_entity.entity_id == identity
    restored = wire_state(hass, new_entity)
    assert restored["state"] == original["state"]
    assert restored["attributes"]["gesture_session_id"] == original["attributes"]["bridge_session_id"]
    assert restored["attributes"]["bridge_session_id"] != restored["attributes"]["gesture_session_id"]
    assert restored["attributes"]["bridge_update_kind"] == "snapshot"
    await source_press(hass)
    fresh = wire_state(hass, new_entity)
    assert fresh["attributes"]["gesture_sequence"] == 1
    assert fresh["attributes"]["gesture_session_id"] == fresh["attributes"]["bridge_session_id"]


async def test_unmapped_action_does_not_publish_a_gesture(hass, ready_sources, caplog):
    _, entity = await add_bridge(hass)
    await source_press(hass, "UnmappedAction")
    state = wire_state(hass, entity)
    assert state["state"] == "unknown"
    assert "gesture_sequence" not in state["attributes"]
    assert "Unmapped action" in caplog.text


async def test_source_recovery_is_baselined(hass, ready_sources):
    _, entity = await add_bridge(hass)
    await source_press(hass)
    first = wire_state(hass, entity)
    hass.states.async_set(SOURCE, "unavailable")
    await hass.async_block_till_done()
    state = wire_state(hass, entity)
    assert state["state"] == first["state"]
    assert state["attributes"]["bridge_sources"][0]["ready"] is False
    hass.states.async_set(SOURCE, first["state"], {"event_type": "KeyPressed2x"})
    await hass.async_block_till_done()
    assert wire_state(hass, entity)["attributes"]["gesture_sequence"] == 1
    await source_press(hass)
    assert wire_state(hass, entity)["attributes"]["gesture_sequence"] == 2


async def test_removing_bridge_unsubscribes_source_and_load(hass, ready_sources):
    _, entity = await add_bridge(hass)
    await entity.async_remove()
    await source_press(hass)
    hass.states.async_set(LOAD, "on")
    await hass.async_block_till_done()
    placeholder = hass.states.get(entity.entity_id)
    assert placeholder is None or placeholder.attributes.get("restored") is True
    assert entity._sequence == 0


async def test_remote_without_load(hass, ready_sources):
    config = deepcopy(CONFIG)
    del config["load_entity_id"]
    _, entity = await add_bridge(hass, config)
    await source_press(hass)
    assert wire_state(hass, entity)["attributes"]["bridge_load"] is None


async def test_bridge_outputs_cannot_form_native_input_cycle(hass):
    config_a = {**deepcopy(CONFIG), "name": "Control A", "unique_id": "control-a", "source_entity_id": "event.control_b", "actions": {"press": {"kind": "press", "press_count": 2}}}
    config_b = {**deepcopy(config_a), "name": "Control B", "unique_id": "control-b", "source_entity_id": "event.control_a"}
    _, a = await add_bridge(hass, config_a)
    _, b = await add_bridge(hass, config_b)
    await asyncio.wait_for(hass.async_block_till_done(), timeout=2)
    assert not a._source_ready and not b._source_ready
    assert "aggregate cannot" in wire_state(hass, a)["attributes"]["bridge_sources"][0]["error"]
    assert "aggregate cannot" in wire_state(hass, b)["attributes"]["bridge_sources"][0]["error"]
    a._trigger_event("press", {"press_count": 2})
    a.async_write_ha_state()
    await asyncio.wait_for(hass.async_block_till_done(), timeout=2)
    assert a._sequence == b._sequence == 0


@pytest.mark.parametrize("field,value", [
    ("source_entity_id", "switch.wrong_domain"),
    ("load_entity_id", "cover.not_supported"),
    ("button_id", "Top Paddle"),
    ("actions", {}),
    ("actions", {"x": {"kind": "press", "press_count": True}}),
    ("actions", {"x": {"kind": "release", "press_count": 1}}),
    ("actions", {"x": {"kind": "custom"}}),
])
def test_invalid_native_config(field, value):
    config = deepcopy(CONFIG)
    config[field] = value
    with pytest.raises(ValueError):
        NativeControlConfig.parse(config)


async def test_setup_rejects_invalid_mapping(hass):
    config = deepcopy(CONFIG)
    config["actions"] = {}
    with pytest.raises(vol.Invalid, match="At least one action"):
        await async_setup_platform(hass, config, lambda entities: None)


@pytest.mark.parametrize("runtime", ["luajit", "lua"])
@pytest.mark.parametrize("entrypoint", ["tests/lua/test_gestures.lua", "tests/controller/test_switch_driver.lua"])
async def test_real_ha_publications_pass_through_lua_proxy_decoder(hass, ready_sources, tmp_path, runtime, entrypoint):
    executable = shutil.which(runtime)
    if executable is None:
        pytest.skip(f"Install {runtime} to run the cross-runtime contract check")
    component, entity = await add_bridge(hass)
    suite = {"entity_id": entity.entity_id, "cases": []}

    def capture(name, expected=None, delivery="event"):
        suite["cases"].append({"name": name, "delivery": delivery, "state": wire_state(hass, entity), "expected_event": expected})

    capture("real_initial_state", delivery="state")
    await source_press(hass)
    capture("real_first_double_press", "top/press_2")
    capture("real_duplicate_delivery")
    await source_press(hass)
    capture("real_second_double_press", "top/press_2")
    old_live = deepcopy(suite["cases"][-1]["state"])
    hass.states.async_set(LOAD, "on")
    await hass.async_block_till_done()
    capture("real_load_snapshot")
    capture("real_rest_refresh", delivery="state")
    old_entity = entity
    await entity.async_remove()
    await hass.async_block_till_done()
    entity = GestureBridgeEvent(NativeControlConfig.parse(CONFIG))
    await component.async_add_entities([entity])
    await hass.async_block_till_done()
    assert entity.entity_id == old_entity.entity_id
    capture("real_restored_evententity", delivery="state")
    await source_press(hass, "KeyPressed3x")
    capture("real_fresh_triple_after_restore", "top/press_3")
    suite["cases"].append({"name": "real_delayed_old_rest_response", "delivery": "state", "state": old_live, "expected_event": None})
    path = tmp_path / "ha-produced-suite.json"
    path.write_text(json.dumps(suite))
    root = consumer_root()
    result = await hass.async_add_executor_job(
        lambda: subprocess.run([executable, entrypoint, str(path)], cwd=root, capture_output=True, text=True, timeout=15)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "9 states, 3 fired gestures" in result.stdout
