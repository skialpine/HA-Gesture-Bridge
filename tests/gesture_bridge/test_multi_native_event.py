"""Real HA upper/lower sources preserve the existing aggregate and v1 envelope."""
from copy import deepcopy
import json
import logging
import shutil

from homeassistant.components.event import EventEntity
from homeassistant.helpers.entity_component import EntityComponent
import pytest

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent, PLATFORM_SCHEMA, async_setup_platform
from tools.validate_gesture_contract import validate_state


TOP = "event.upper_office_paddle"
BOTTOM = "event.lower_office_paddle"
LOAD = "switch.office_relay"
ACTIONS = {
    "KeyPressed": {"kind": "press", "press_count": 1},
    "KeyPressed2x": {"kind": "press", "press_count": 2},
    "KeyPressed3x": {"kind": "press", "press_count": 3},
    "KeyHeldDown": {"kind": "hold_start"},
    "KeyReleased": {"kind": "release"},
}
CONFIG = {"platform": "ha_gesture_bridge", "unique_id": "existing-office-control", "name": "Office Control",
          "load_entity_id": LOAD,
          "buttons": [{"id": "top", "label": "Top", "source_entity_id": TOP, "actions": deepcopy(ACTIONS)},
                      {"id": "bottom", "label": "Bottom", "source_entity_id": BOTTOM, "actions": deepcopy(ACTIONS)}]}


class Paddle(EventEntity):
    _attr_event_types = list(ACTIONS)

    def __init__(self, name):
        self._attr_name = name

    async def press(self, action):
        self._trigger_event(action)
        self.async_write_ha_state()
        await self.hass.async_block_till_done()


def wire(hass, entity):
    state = json.loads(json.dumps(hass.states.get(entity.entity_id).as_dict(), default=lambda value: value.isoformat()))
    validate_state(state)
    return state


async def setup(hass, config=None):
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    top, bottom = Paddle("Upper Office Paddle"), Paddle("Lower Office Paddle")
    await component.async_add_entities([top, bottom])
    assert top.entity_id == TOP and bottom.entity_id == BOTTOM
    hass.states.async_set(LOAD, "off")
    entities = []
    await async_setup_platform(hass, PLATFORM_SCHEMA(deepcopy(config or CONFIG)), entities.extend)
    aggregate = entities[0]
    await component.async_add_entities([aggregate])
    await hass.async_block_till_done()
    return component, aggregate, top, bottom


def test_legacy_shorthand_and_stable_ids():
    legacy = NativeControlConfig.parse({"unique_id": CONFIG["unique_id"], "name": CONFIG["name"],
                                      "source_entity_id": TOP, "actions": ACTIONS})
    multi = NativeControlConfig.parse(CONFIG)
    assert multi.control_id == legacy.control_id
    assert multi.buttons()[0] == legacy.buttons()[0]
    assert multi.buttons()[1]["id"] == "bottom"
    assert "hold_repeat" in {gesture["id"] for gesture in multi.buttons()[0]["gestures"]}
    assert not any(gesture["label"] == "Button Pushed" for button in multi.buttons() for gesture in button["gestures"])


@pytest.mark.parametrize("change", [
    lambda c: c.update(source_entity_id=TOP), lambda c: c.update(actions=ACTIONS),
    lambda c: c.update(button_id="top"), lambda c: c.update(button_label="Top"),
    lambda c: c.update(buttons=[]), lambda c: c.update(buttons={"top": {}}),
    lambda c: c["buttons"][1].update(id="top"), lambda c: c["buttons"][1].update(source_entity_id=TOP),
    lambda c: c["buttons"][1].update(source_entity_id="switch.not_an_event"),
    lambda c: c["buttons"][1].update(actions={}),
    lambda c: c["buttons"][1].update(button_id="wrong_field"),
    lambda c: c["buttons"][0]["actions"].update(x={"kind": "custom", "id": "hold_repeat"}),
])
def test_ambiguous_or_invalid_multibutton_config(change):
    config = deepcopy(CONFIG)
    change(config)
    with pytest.raises(ValueError):
        NativeControlConfig.parse(config)


@pytest.mark.parametrize("button,action,count", [(button, action, count) for button in ("top", "bottom")
                                                  for action, count in (("KeyPressed", 1), ("KeyPressed2x", 2), ("KeyPressed3x", 3))])
async def test_real_upper_and_lower_taps(hass, button, action, count):
    _, entity, top, bottom = await setup(hass)
    source = top if button == "top" else bottom
    initial = wire(hass, entity)
    assert initial["state"] == "unknown" and "gesture_sequence" not in initial["attributes"]
    await source.press(action)
    first = wire(hass, entity)
    assert first["attributes"]["button_id"] == button
    assert first["attributes"]["gesture_id"] == f"press_{count}"
    assert first["attributes"]["press_count"] == count
    await source.press(action)
    repeated = wire(hass, entity)
    assert repeated["attributes"]["gesture_sequence"] == 2
    assert repeated["attributes"]["gesture_event_id"] != first["attributes"]["gesture_event_id"]


async def test_independent_hold_sessions_and_only_reported_releases(hass):
    _, entity, top, bottom = await setup(hass)
    observed = []
    for source, action in ((top, "KeyHeldDown"), (bottom, "KeyHeldDown"), (top, "KeyHeldDown"),
                           (bottom, "KeyHeldDown"), (top, "KeyReleased"), (bottom, "KeyReleased"), (top, "KeyHeldDown")):
        await source.press(action)
        attrs = wire(hass, entity)["attributes"]
        observed.append((attrs["button_id"], attrs["event_type"], attrs["gesture_id"]))
    assert observed == [("top", "hold_start", "hold_start"), ("bottom", "hold_start", "hold_start"),
                        ("top", "hold_repeat", "hold_repeat"), ("bottom", "hold_repeat", "hold_repeat"),
                        ("top", "release", "release"), ("bottom", "release", "release"), ("top", "hold_start", "hold_start")]
    assert entity._sequence == 7


async def test_source_failure_recovery_and_load_snapshots_are_independent(hass):
    _, entity, top, bottom = await setup(hass)
    await top.press("KeyHeldDown")
    await bottom.press("KeyHeldDown")
    last = wire(hass, entity)
    hass.states.async_set(TOP, "unavailable")
    await hass.async_block_till_done()
    unavailable = wire(hass, entity)
    status = {source["button_id"]: source for source in unavailable["attributes"]["bridge_sources"]}
    assert status["top"]["id"] == "native-top" and not status["top"]["ready"]
    assert status["bottom"]["id"] == "native-bottom" and status["bottom"]["ready"]
    assert unavailable["attributes"]["gesture_event_id"] == last["attributes"]["gesture_event_id"]
    assert unavailable["attributes"]["bridge_update_kind"] == "snapshot"
    await bottom.press("KeyHeldDown")
    assert wire(hass, entity)["attributes"]["event_type"] == "hold_repeat"
    # Recovery's preserved event timestamp establishes a baseline, no release.
    hass.states.async_set(TOP, last["state"], {"event_type": "KeyReleased"})
    await hass.async_block_till_done()
    assert entity._sequence == 3
    await top.press("KeyHeldDown")
    fresh = wire(hass, entity)
    assert fresh["attributes"]["event_type"] == "hold_start"
    assert fresh["attributes"]["button_id"] == "top"
    for state in ("on", "unavailable", "off"):
        hass.states.async_set(LOAD, state)
        await hass.async_block_till_done()
        changed = wire(hass, entity)
        assert changed["state"] == fresh["state"]
        assert changed["attributes"]["gesture_event_id"] == fresh["attributes"]["gesture_event_id"]
        assert changed["attributes"]["bridge_update_kind"] == "snapshot"
    await bottom.press("KeyReleased")
    assert wire(hass, entity)["attributes"]["button_id"] == "bottom"


async def test_metadata_and_unknown_sources_do_not_replay(hass):
    _, entity, top, bottom = await setup(hass)
    await top.press("KeyPressed2x")
    last = wire(hass, entity)
    source = hass.states.get(TOP)
    hass.states.async_set(TOP, source.state, {"event_type": "KeyPressed3x", "new_metadata": True})
    hass.states.async_set("event.unselected", last["state"], {"event_type": "KeyPressed3x"})
    await hass.async_block_till_done()
    assert entity._sequence == 1
    await bottom.press("KeyPressed3x")
    assert entity._sequence == 2
    assert wire(hass, entity)["attributes"]["button_id"] == "bottom"


async def test_reload_preserves_entity_and_old_gesture_ids_without_replay(hass):
    component, entity, top, bottom = await setup(hass)
    await top.press("KeyPressed2x")
    previous = wire(hass, entity)
    await entity.async_remove()
    replacement = GestureBridgeEvent(NativeControlConfig.parse(CONFIG))
    await component.async_add_entities([replacement])
    await hass.async_block_till_done()
    restored = wire(hass, replacement)
    assert replacement.entity_id == entity.entity_id
    assert restored["attributes"]["bridge_control_id"] == previous["attributes"]["bridge_control_id"]
    assert restored["attributes"]["gesture_event_id"] == previous["attributes"]["gesture_event_id"]
    assert restored["attributes"]["bridge_session_id"] != previous["attributes"]["bridge_session_id"]
    assert restored["attributes"]["bridge_update_kind"] == "snapshot" and replacement._sequence == 0
    await bottom.press("KeyHeldDown")
    assert wire(hass, replacement)["attributes"]["event_type"] == "hold_start"
    assert replacement._sequence == 1


async def test_removal_cleans_both_sources_load_and_hold_state(hass):
    _, entity, top, bottom = await setup(hass)
    await top.press("KeyHeldDown")
    await bottom.press("KeyHeldDown")
    before = entity._sequence
    await entity.async_remove()
    assert not entity._held_buttons
    await top.press("KeyReleased")
    await bottom.press("KeyPressed3x")
    hass.states.async_set(LOAD, "on")
    await hass.async_block_till_done()
    assert entity._sequence == before


async def test_upgrade_legacy_top_shorthand_to_two_buttons_preserves_control(hass):
    legacy = {"platform": "ha_gesture_bridge", "unique_id": CONFIG["unique_id"], "name": CONFIG["name"],
              "source_entity_id": TOP, "load_entity_id": LOAD,
              "actions": {key: deepcopy(ACTIONS[key]) for key in ("KeyPressed2x", "KeyPressed3x")}}
    component, entity, top, bottom = await setup(hass, legacy)
    await top.press("KeyPressed2x")
    original = wire(hass, entity)
    assert original["attributes"]["bridge_sources"][0]["id"] == "native-source"
    await entity.async_remove()
    replacement = GestureBridgeEvent(NativeControlConfig.parse(CONFIG))
    await component.async_add_entities([replacement])
    await hass.async_block_till_done()
    upgraded = wire(hass, replacement)
    assert replacement.entity_id == entity.entity_id
    assert upgraded["attributes"]["bridge_control_id"] == original["attributes"]["bridge_control_id"]
    assert upgraded["attributes"]["button_id"] == "top" and upgraded["attributes"]["gesture_id"] == "press_2"
    assert upgraded["attributes"]["gesture_event_id"] == original["attributes"]["gesture_event_id"]
    assert upgraded["attributes"]["bridge_update_kind"] == "snapshot" and replacement._sequence == 0
    await bottom.press("KeyPressed3x")
    assert wire(hass, replacement)["attributes"]["button_id"] == "bottom"
    assert wire(hass, replacement)["attributes"]["gesture_sequence"] == 1


async def test_bad_source_clears_only_its_hold_and_other_button_stays_usable(hass):
    _, entity, top, bottom = await setup(hass)
    await top.press("KeyHeldDown")
    source = hass.states.get(TOP)
    hass.states.async_set(TOP, source.state, {"bridge_contract_version": "1.0"})
    await hass.async_block_till_done()
    status = {source["button_id"]: source for source in wire(hass, entity)["attributes"]["bridge_sources"]}
    assert not status["top"]["ready"] and "aggregate cannot" in status["top"]["error"]
    assert status["bottom"]["ready"] and "top" not in entity._held_buttons
    assert entity._sequence == 1
    await bottom.press("KeyPressed2x")
    assert wire(hass, entity)["attributes"]["button_id"] == "bottom" and entity._sequence == 2
    await top.press("KeyHeldDown")
    assert wire(hass, entity)["attributes"]["event_type"] == "hold_start"


@pytest.mark.parametrize("runtime", ["luajit", "lua"])
async def test_multi_source_publications_pass_actual_lua_decoder(hass, tmp_path, runtime, run_consumer):
    executable = shutil.which(runtime)
    if executable is None:
        pytest.skip(f"Install {runtime} to check the C4 boundary")
    _, entity, top, bottom = await setup(hass)
    suite = {"entity_id": entity.entity_id, "cases": []}
    def capture(expected=None, delivery="event"):
        suite["cases"].append({"name": f"multi_{len(suite['cases'])}", "delivery": delivery,
                               "state": wire(hass, entity), "expected_event": expected})
    capture(delivery="state")
    for source, button in ((top, "top"), (bottom, "bottom")):
        for action, gesture in (("KeyPressed", "press_1"), ("KeyPressed2x", "press_2"), ("KeyPressed3x", "press_3"),
                                ("KeyHeldDown", "hold_start"), ("KeyHeldDown", "hold_repeat"), ("KeyReleased", "release")):
            await source.press(action)
            capture(f"{button}/{gesture}")
            capture()  # Duplicate transport cannot replay the same occurrence.
    hass.states.async_set(LOAD, "on")
    await hass.async_block_till_done()
    capture()
    path = tmp_path / "multi-native.json"
    path.write_text(json.dumps(suite))
    await run_consumer(executable, "tests/lua/test_gestures.lua", path)


@pytest.mark.parametrize("lost_state", ["unknown", "invalid-timestamp"])
async def test_lost_event_tracking_invalidates_only_that_source_hold(hass, lost_state):
    _, entity, top, bottom = await setup(hass)
    initial = wire(hass, entity)
    assert all(source["ready"] for source in initial["attributes"]["bridge_sources"])
    await top.press("KeyHeldDown")
    held = wire(hass, entity)
    source = hass.states.get(TOP)
    # A duplicate/attribute-only source update must not clear an active hold.
    hass.states.async_set(TOP, source.state, {"event_type": "KeyHeldDown", "metadata": True})
    await hass.async_block_till_done()
    assert "top" in entity._held_buttons and entity._sequence == 1
    hass.states.async_set(TOP, lost_state)
    await hass.async_block_till_done()
    lost = wire(hass, entity)
    statuses = {source["button_id"]: source for source in lost["attributes"]["bridge_sources"]}
    assert statuses["top"]["ready"] is False and statuses["top"]["availability"] == "unknown"
    assert statuses["bottom"]["ready"] is True
    assert "top" not in entity._held_buttons
    assert lost["state"] == held["state"]
    assert lost["attributes"]["gesture_event_id"] == held["attributes"]["gesture_event_id"]
    assert lost["attributes"]["event_type"] == "hold_start" and entity._sequence == 1
    assert lost["attributes"]["bridge_update_kind"] == "snapshot"
    await top.press("KeyHeldDown")
    recovered = wire(hass, entity)
    assert recovered["attributes"]["event_type"] == "hold_start" and entity._sequence == 2
    assert recovered["attributes"]["bridge_sources"][0]["ready"] is True
    await top.press("KeyHeldDown")
    assert wire(hass, entity)["attributes"]["event_type"] == "hold_repeat" and entity._sequence == 3


@pytest.mark.parametrize("runtime", ["luajit", "lua"])
async def test_real_ha_sources_drive_actual_switch_button_links(hass, tmp_path, runtime, run_consumer):
    executable = shutil.which(runtime)
    if executable is None:
        pytest.skip(f"Install {runtime} to check actual Switch outputs")
    _, entity, top, bottom = await setup(hass)
    suite = {"entity_id": entity.entity_id, "cases": []}

    def capture(expected=None, links=(), delivery="event"):
        suite["cases"].append({"name": f"real_controller_{len(suite['cases'])}", "delivery": delivery,
            "state": wire(hass, entity), "expected_event": expected,
            "expected_links": [{"binding": binding, "command": command} for binding, command in links]})

    capture(delivery="state")
    await top.press("KeyPressed")
    capture("top/press_1", ((200, "DO_PUSH"), (200, "DO_CLICK")))
    await top.press("KeyPressed2x")
    capture("top/press_2")
    await top.press("KeyHeldDown")
    capture("top/hold_start", ((200, "DO_PUSH"),))
    await top.press("KeyHeldDown")
    capture("top/hold_repeat")
    hass.states.async_set(TOP, "unknown")
    await hass.async_block_till_done()
    capture(links=((200, "DO_RELEASE"),))
    await bottom.press("KeyPressed")
    capture("bottom/press_1", ((201, "DO_PUSH"), (201, "DO_CLICK")))
    await bottom.press("KeyReleased")
    capture("bottom/release")  # Genuine source release, no active C4 hold.
    await top.press("KeyHeldDown")
    capture("top/hold_start", ((200, "DO_PUSH"),))
    capture()  # Duplicate transport must neither replay nor release the hold.
    await top.press("KeyReleased")
    capture("top/release", ((200, "DO_RELEASE"),))
    path = tmp_path / "real-native-controller.json"
    path.write_text(json.dumps(suite))
    await run_consumer(executable, "tests/controller/test_switch_driver.lua", path, "controller")
