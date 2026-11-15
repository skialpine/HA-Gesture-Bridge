"""Additive 1.1 light snapshots retain the 1.0 gesture and switch contracts."""
from copy import deepcopy
import json
import logging
from pathlib import Path
import shutil
import subprocess
import os

from homeassistant.components.event import EventEntity
from homeassistant.helpers.entity_component import EntityComponent
import pytest
from jsonschema import ValidationError

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent
from tools.validate_gesture_contract import VALIDATOR, validate_state



def consumer_root():
    checkout = os.environ.get("C4_HA_SWITCH_CHECKOUT")
    if not checkout:
        pytest.skip("Set C4_HA_SWITCH_CHECKOUT to run optional C4 consumer checks")
    root = Path(checkout).resolve()
    if not (root / "module/gestures.lua").is_file():
        pytest.fail("C4_HA_SWITCH_CHECKOUT does not contain the gesture consumer")
    return root

LIGHT = "light.office_brightness"
ATTRS = {"supported_color_modes": ["brightness"], "supported_features": 32, "brightness": 128,
         "friendly_name": "Office", "private": {"do_not_project": True}}
CONFIG = {"unique_id": "existing-control", "name": "Existing Control", "source_entity_id": "event.light_paddle",
          "load_entity_id": LIGHT, "actions": {"double": {"kind": "press", "press_count": 2}}}


class Paddle(EventEntity):
    _attr_name = "Light Paddle"
    _attr_event_types = ["double"]

    async def press(self):
        self._trigger_event("double")
        self.async_write_ha_state()
        await self.hass.async_block_till_done()


async def setup(hass, load=LIGHT):
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    source = Paddle()
    await component.async_add_entities([source])
    hass.states.async_set(load, "off", deepcopy(ATTRS))
    entity = GestureBridgeEvent(NativeControlConfig.parse({**CONFIG, "load_entity_id": load}))
    await component.async_add_entities([entity])
    await hass.async_block_till_done()
    return source, entity


def wire(hass, entity):
    state = json.loads(json.dumps(hass.states.get(entity.entity_id).as_dict(), default=lambda value: value.isoformat()))
    validate_state(state)
    return state


async def test_light_load_projects_only_typed_capability_and_brightness_attributes(hass):
    _, entity = await setup(hass)
    attrs = wire(hass, entity)["attributes"]
    assert attrs["bridge_contract_version"] == "1.1"
    assert attrs["bridge_load"] == {"entity_id": LIGHT, "domain": "light", "state": "off", "available": True,
                                     "attributes": {key: ATTRS[key] for key in ("supported_color_modes", "supported_features", "brightness")}}
    assert attrs["bridge_control_id"] == CONFIG["unique_id"]
    assert attrs["bridge_buttons"][0]["gestures"][0]["id"] == "press_2"
    assert "gesture_sequence" not in attrs


async def test_light_changes_never_create_or_replay_a_gesture(hass):
    source, entity = await setup(hass)
    await source.press()
    first = wire(hass, entity)
    for value, attrs in (("on", {**ATTRS, "brightness": 64}), ("on", {**ATTRS, "brightness": 191}),
                         ("off", {**ATTRS, "brightness": 255}), ("unavailable", {}), ("unknown", {}), ("on", ATTRS)):
        hass.states.async_set(LIGHT, value, deepcopy(attrs))
        await hass.async_block_till_done()
        current = wire(hass, entity)
        snapshot = current["attributes"]["bridge_load"]
        assert snapshot["state"] == value and snapshot["available"] == (value in ("on", "off"))
        assert snapshot["attributes"].get("brightness") == attrs.get("brightness")
        assert current["state"] == first["state"]
        assert current["attributes"]["gesture_sequence"] == first["attributes"]["gesture_sequence"]
        assert current["attributes"]["gesture_event_id"] == first["attributes"]["gesture_event_id"]
        assert current["attributes"]["bridge_update_kind"] == "snapshot"
    await source.press()
    assert wire(hass, entity)["attributes"]["gesture_sequence"] == 2


@pytest.mark.parametrize("field,value", [
    ("brightness", "128"), ("brightness", True), ("brightness", -1), ("brightness", 256),
    ("brightness", float("nan")), ("brightness", float("inf")),
    ("brightness", 10**400),
    ("supported_features", True), ("supported_features", -1), ("supported_features", 1.5),
    ("supported_color_modes", {}), ("supported_color_modes", ["future_mode"]),
    ("supported_color_modes", ["brightness", "brightness"]),
])
async def test_bad_upstream_attribute_does_not_grant_a_capability(hass, field, value):
    _, entity = await setup(hass)
    bad = deepcopy(ATTRS)
    bad[field] = value
    hass.states.async_set(LIGHT, "on", bad)
    await hass.async_block_till_done()
    assert field not in wire(hass, entity)["attributes"]["bridge_load"]["attributes"]


@pytest.mark.parametrize("load", ["switch.old_load", "input_boolean.old_load"])
async def test_existing_switch_producer_stays_contract_1_0(hass, load):
    _, entity = await setup(hass, load)
    attrs = wire(hass, entity)["attributes"]
    assert attrs["bridge_contract_version"] == "1.0"
    assert attrs["bridge_load"] == {"entity_id": load, "domain": load.split(".")[0], "state": "off", "available": True}


@pytest.mark.parametrize("runtime", ["luajit", "lua"])
async def test_real_light_snapshots_pass_existing_lua_consumer(hass, tmp_path, runtime):
    executable = shutil.which(runtime)
    if executable is None:
        pytest.skip(f"Install {runtime}")
    source, entity = await setup(hass)
    suite = {"entity_id": entity.entity_id, "cases": []}
    def capture(name, expected=None, delivery="event"):
        suite["cases"].append({"name": name, "delivery": delivery, "state": wire(hass, entity), "expected_event": expected})
    capture("initial_light", delivery="state")
    await source.press()
    capture("double_with_light", "top/press_2")
    for index, value in enumerate(("on", "off", "unavailable", "on")):
        hass.states.async_set(LIGHT, value, deepcopy(ATTRS) if value != "unavailable" else {})
        await hass.async_block_till_done()
        capture(f"light_snapshot_{index}")
    await source.press()
    capture("fresh_after_light_recovery", "top/press_2")
    path = tmp_path / "light-producer.json"
    path.write_text(json.dumps(suite))
    subprocess.run([executable, "tests/lua/test_gestures.lua", str(path)], cwd=consumer_root(), check=True, capture_output=True, text=True)
    subprocess.run([executable, "tests/controller/test_dimmer_light_publications.lua", str(path)],
                   cwd=consumer_root(), check=True, capture_output=True, text=True)


@pytest.mark.parametrize("runtime", ["luajit", "lua"])
def test_light_wire_shape_rejection_uses_real_private_decoder(tmp_path, runtime):
    executable = shutil.which(runtime)
    if executable is None:
        pytest.skip(f"Install {runtime}")
    baseline = json.loads(Path("tests/fixtures/gestures/v1.json").read_text())["cases"][0]["state"]
    baseline["attributes"]["bridge_contract_version"] = "1.1"
    baseline["attributes"]["bridge_load"] = {"entity_id": LIGHT, "domain": "light", "state": "on", "available": True,
                                              "attributes": {key: ATTRS[key] for key in ("supported_color_modes", "supported_features", "brightness")}}
    cases = []
    def append(name, state, valid):
        if valid:
            validate_state(state)
        else:
            with pytest.raises((ValidationError, ValueError)):
                validate_state(state)
        cases.append({"name": name, "state": state, "valid": valid})
    append("valid_1_1_light", deepcopy(baseline), True)
    for attributes in ({}, {"supported_color_modes": []}, {"supported_color_modes": ["onoff"]}):
        state = deepcopy(baseline); state["attributes"]["bridge_load"]["attributes"] = attributes
        append("missing_or_empty_caps_are_conservative", state, True)
    invalid = [None, [], {"brightness": None}, {"brightness": False}, {"brightness": "128"},
               {"brightness": -1}, {"brightness": 256}, {"supported_features": "32"},
               {"supported_features": False}, {"supported_features": -1}, {"supported_features": 1.5},
               {"supported_features": 4294967296}, {"supported_color_modes": {}}, {"supported_color_modes": None},
               {"supported_color_modes": ["brightness", "brightness"]}, {"supported_color_modes": ["not_a_mode"]},
               {"private": "must_not_cross_contract"}]
    for index, attributes in enumerate(invalid):
        state = deepcopy(baseline); state["attributes"]["bridge_load"]["attributes"] = attributes
        append(f"invalid_attributes_{index}", state, False)
    absent = deepcopy(baseline); del absent["attributes"]["bridge_load"]["attributes"]
    append("light_attributes_required", absent, False)
    old_version = deepcopy(baseline); old_version["attributes"]["bridge_contract_version"] = "1.0"
    append("light_requires_minor_1", old_version, False)
    mismatch = deepcopy(baseline); mismatch["attributes"]["bridge_load"]["entity_id"] = "switch.wrong"
    append("domain_identity_mismatch", mismatch, False)
    fixture = tmp_path / "strict-light-cases.json"
    fixture.write_text(json.dumps(cases))
    runner = tmp_path / "strict-light.lua"
    runner.write_text('''local Discovery=require('module.discovery')
local Gestures=require('module.gestures')
local codec=Discovery.new_codec()
local file=assert(io.open(arg[1],'r')); local cases=codec:decode(file:read('*a')); file:close()
local fires=0
local consumer=Gestures.new({entity_id=cases[1].state.entity_id,c4={
 AddEvent=function() return true end,FireEventByID=function() fires=fires+1 end,
 SendToProxy=function() error('Validation cannot operate a load') end}})
consumer:start()
for _,case in ipairs(cases) do
 local fired=consumer:consume_proxy('RECEIEVE_STATE',{response=codec:encode(case.state)})
 assert(not fired and fires==0,case.name..': snapshot fired')
 assert(consumer.ready==case.valid,case.name..': incorrect metadata readiness')
 assert((consumer.last_state~=nil)==case.valid,case.name..': retained malformed metadata')
end
print('Strict light bridge wire cases passed: '..#cases)
''')
    subprocess.run([executable, str(runner), str(fixture)], cwd=consumer_root(), check=True, capture_output=True, text=True)
