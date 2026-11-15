"""Exercise input normalization against actual HA state/event/entity machinery.

MQTT tests replace only HA's broker subscription boundary: the core, entity,
state machine, dispatcher and MQTT ReceiveMessage model are real.
"""

import asyncio
import json
import logging

from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
import pytest

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent
from custom_components.ha_gesture_bridge.sources import matches
from tools.validate_gesture_contract import validate_state


async def add(hass, sources, load=None):
    config = {"unique_id": "source-fixture", "name": "Source Fixture", "sources": sources}
    if load:
        config["load_entity_id"] = load
    entity = GestureBridgeEvent(NativeControlConfig.parse(config))
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([entity])
    await hass.async_block_till_done()
    return entity


def wire(hass, entity):
    state = json.loads(json.dumps(hass.states.get(entity.entity_id).as_dict(), default=str))
    validate_state(state)
    return state["attributes"]


def mapping(button, match, kind="press", count=1):
    gesture = {"kind": kind}
    if kind == "press":
        gesture["press_count"] = count
    return {"button_id": button, "match": match, "gesture": gesture}


def zwave(**changes):
    return {"id": "paddle", "type": "zwave_js", "device_id": "ha-device",
            "buttons": [{"id": "top", "label": "Top", "scene": 1}, {"id": "bottom", "label": "Bottom", "scene": 2}], **changes}


def bus(**changes):
    return {"id": "remote", "type": "bus", "event_type": "vendor_button", "filters": {"serial": "explicit-device"},
            "buttons": [{"id": "top", "label": "Top"}],
            "mappings": [mapping("top", {"action": "click"})], **changes}


async def fire(hass, event_type, data):
    hass.bus.async_fire(event_type, data)
    await hass.async_block_till_done()


async def zpress(hass, value, scene="001", **changes):
    await fire(hass, "zwave_js_value_notification", {"device_id": "ha-device", "command_class": 91, "property": "scene",
                                                   "property_key": scene, "value": value, **changes})


@pytest.mark.parametrize("count", range(1, 6))
@pytest.mark.parametrize("scene,button", [("001", "top"), (2, "bottom")])
async def test_zwave_counts_one_through_five_and_identical_occurrences(hass, count, scene, button):
    entity = await add(hass, [zwave()])
    assert "gesture_sequence" not in wire(hass, entity)
    action = "KeyPressed" if count == 1 else f"KeyPressed{count}x"
    for sequence in (1, 2):
        await zpress(hass, action, scene)
        attrs = wire(hass, entity)
        assert attrs["button_id"] == button
        assert attrs["gesture_id"] == f"press_{count}"
        assert attrs["press_count"] == count
        assert attrs["gesture_sequence"] == sequence


async def test_zwave_strict_identity_class_scene_hold_repeat_release(hass):
    entity = await add(hass, [zwave()])
    for change in ({"device_id": "other"}, {"command_class": 92}, {"command_class": True}, {"property": "other"}, {"property_key": "bad"}):
        await zpress(hass, "KeyPressed", **change)
    assert "gesture_sequence" not in wire(hass, entity)
    for action, scene, expected, sequence in [("KeyHeldDown", 1, "hold_start", 1), ("KeyHeldDown", 1, "hold_repeat", 2),
                                               ("KeyHeldDown", 2, "hold_start", 3), ("KeyReleased", 1, "release", 4),
                                               ("KeyHeldDown", 2, "hold_repeat", 5)]:
        await zpress(hass, action, scene)
        attrs = wire(hass, entity)
        assert attrs["event_type"] == expected
        assert attrs["gesture_sequence"] == sequence
    await zpress(hass, "unsupported", 1)
    assert wire(hass, entity)["gesture_sequence"] == 5
    assert wire(hass, entity)["bridge_update_kind"] == "snapshot"
    assert wire(hass, entity)["bridge_unmapped_count"] == 1


async def test_bus_declarative_paths_typed_filters_and_cleanup(hass):
    source = bus(mappings=[mapping("top", {"args.0": 7, "args.1.action": "click", "ok": True})])
    entity = await add(hass, [source])
    good = {"serial": "explicit-device", "args": [7, {"action": "click"}], "ok": True}
    for data in ({**good, "serial": "other"}, {**good, "ok": 1}, {**good, "args": []}):
        await fire(hass, "vendor_button", data)
    assert "gesture_sequence" not in wire(hass, entity)
    await fire(hass, "vendor_button", good)
    assert wire(hass, entity)["gesture_sequence"] == 1
    await entity.async_remove()
    await fire(hass, "vendor_button", good)
    assert entity._sequence == 1
    assert not entity._held_buttons


async def test_zha_maps_only_explicit_device_command_and_args(hass):
    source = {"id": "zigbee", "type": "zha_event", "device_id": "ha-zigbee-device",
              "buttons": [{"id": "left", "label": "Left"}],
              "mappings": [mapping("left", {"command": "vendor_click", "args.0": 2}, count=2),
                           mapping("left", {"command": "vendor_hold"}, kind="hold_start"),
                           mapping("left", {"command": "vendor_release"}, kind="release")]}
    entity = await add(hass, [source])
    await fire(hass, "zha_event", {"device_id": "other", "command": "vendor_click", "args": [2]})
    assert "gesture_sequence" not in wire(hass, entity)
    for command, expected in (("vendor_click", "press"), ("vendor_hold", "hold_start"), ("vendor_hold", "hold_repeat"), ("vendor_release", "release")):
        await fire(hass, "zha_event", {"device_id": "ha-zigbee-device", "command": command, "args": [2]})
        assert wire(hass, entity)["event_type"] == expected
    assert wire(hass, entity)["button_id"] == "left"


async def test_shared_native_entity_baseline_repeats_and_metadata_no_replay(hass):
    source_id = "event.shared_remote"
    hass.states.async_set(source_id, "unknown", {"event_type": None})
    source = {"id": "shared", "type": "shared_native", "source_entity_id": source_id,
              "buttons": [{"id": "top", "label": "Top"}, {"id": "bottom", "label": "Bottom"}],
              "mappings": [mapping("top", {"event_type": "up_double"}, count=2),
                           mapping("bottom", {"event_type": "down_triple"}, count=3)]}
    entity = await add(hass, [source], "light.load")
    for sequence, action, bid in ((1, "up_double", "top"), (2, "up_double", "top"), (3, "down_triple", "bottom")):
        hass.states.async_set(source_id, dt_util.utcnow().isoformat(), {"event_type": action})
        await hass.async_block_till_done()
        assert wire(hass, entity)["gesture_sequence"] == sequence
        assert wire(hass, entity)["button_id"] == bid
    source_state = hass.states.get(source_id)
    hass.states.async_set(source_id, source_state.state, {**source_state.attributes, "battery": 99})
    await hass.async_block_till_done()
    assert wire(hass, entity)["gesture_sequence"] == 3
    assert wire(hass, entity)["bridge_update_kind"] == "snapshot"
    hass.states.async_set("light.load", "on", {"brightness": 42, "supported_color_modes": ["brightness"]})
    await hass.async_block_till_done()
    assert wire(hass, entity)["gesture_sequence"] == 3
    assert wire(hass, entity)["bridge_load"]["attributes"]["brightness"] == 42
    assert {row["button_id"] for row in wire(hass, entity)["bridge_sources"]} == {"top", "bottom"}


async def test_native_source_forms_recovery_cycle_and_unknown_tracking_loss(hass):
    eid = "event.native_source"
    hass.states.async_set(eid, "unknown")
    source = {"id": "native", "type": "native_event", "source_entity_id": eid,
              "actions": {"held": {"kind": "hold_start"}, "released": {"kind": "release"}}}
    entity = await add(hass, [source])
    async def change(state, attrs=None):
        hass.states.async_set(eid, state, attrs or {})
        await hass.async_block_till_done()
    await change(dt_util.utcnow().isoformat(), {"event_type": "held"})
    assert wire(hass, entity)["event_type"] == "hold_start"
    await change("unknown")
    assert wire(hass, entity)["gesture_sequence"] == 1
    assert wire(hass, entity)["bridge_sources"][0]["ready"] is False
    assert wire(hass, entity)["bridge_sources"][0]["availability"] == "unknown"
    await change(dt_util.utcnow().isoformat(), {"event_type": "held"})
    assert wire(hass, entity)["event_type"] == "hold_start"
    await change("unavailable")
    await change(dt_util.utcnow().isoformat(), {"event_type": "held"})
    assert wire(hass, entity)["gesture_sequence"] == 2  # recovery baseline
    await change(dt_util.utcnow().isoformat(), {"event_type": "held"})
    assert wire(hass, entity)["gesture_sequence"] == 3
    await change(dt_util.utcnow().isoformat(), {"event_type": "held", "bridge_contract_version": "1.0"})
    assert wire(hass, entity)["bridge_sources"][0]["error"] == "aggregate_source_forbidden"
    assert wire(hass, entity)["gesture_sequence"] == 3


async def test_inactivity_clears_hold_bookkeeping_without_release(hass):
    entity = await add(hass, [bus(inactivity_timeout=0.1, mappings=[mapping("top", {"action": "held"}, kind="hold_start")])])
    await fire(hass, "vendor_button", {"serial": "explicit-device", "action": "held"})
    assert wire(hass, entity)["event_type"] == "hold_start"
    await asyncio.sleep(0.15)
    await hass.async_block_till_done()
    assert not entity._held_buttons
    assert wire(hass, entity)["gesture_sequence"] == 1
    assert wire(hass, entity)["event_type"] == "hold_start"
    await fire(hass, "vendor_button", {"serial": "explicit-device", "action": "held"})
    assert wire(hass, entity)["gesture_sequence"] == 2
    assert wire(hass, entity)["event_type"] == "hold_start"


@pytest.fixture
def mqtt_boundary(monkeypatch, hass):
    from homeassistant.components import mqtt
    hass.config.components.add("mqtt")
    callbacks, removed = {}, []
    async def subscribe(hass, topic, callback, qos=0):
        callbacks[topic] = callback
        return lambda: removed.append(topic)
    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "is_connected", lambda hass: True)
    # Connection dispatcher remains the real HA implementation.
    return callbacks, removed


def message(topic, payload, retain=False):
    return ReceiveMessage(topic, payload, 0, retain, topic, 0.0)


async def test_mqtt_json_and_string_actions_retained_drop_availability_cleanup(hass, mqtt_boundary):
    callbacks, removed = mqtt_boundary
    source = {"id": "mqtt_remote", "type": "zigbee2mqtt", "topic": "zigbee2mqtt/remote",
              "availability_topic": "zigbee2mqtt/remote/availability", "buttons": [{"id": "top", "label": "Top"}],
              "mappings": [mapping("top", {"action": "double"}, count=2), mapping("top", {"action": "hold"}, kind="hold_start")]}
    entity = await add(hass, [source])
    action, available = callbacks[source["topic"]], callbacks[source["availability_topic"]]
    action(message(source["topic"], '{"action":"double"}', True))
    action(message(source["topic"], '{"action":"double"}'))
    assert "gesture_sequence" not in wire(hass, entity)
    available(message(source["availability_topic"], '{"state":"online"}', True))
    for sequence, payload in ((1, '{"action":"double"}'), (2, "double"), (3, '{"action":"hold"}')):
        action(message(source["topic"], payload))
        assert wire(hass, entity)["gesture_sequence"] == sequence
    action(message(source["topic"], "double", True))
    assert wire(hass, entity)["gesture_sequence"] == 3
    available(message(source["availability_topic"], "offline", True))
    assert not entity._held_buttons
    assert wire(hass, entity)["bridge_sources"][0]["ready"] is False
    action(message(source["topic"], "double"))
    assert wire(hass, entity)["gesture_sequence"] == 3
    available(message(source["availability_topic"], "online"))
    action(message(source["topic"], "double"))
    assert wire(hass, entity)["gesture_sequence"] == 4
    await entity.async_remove()
    assert set(removed) == {source["topic"], source["availability_topic"]}
    action(message(source["topic"], "double"))
    assert entity._sequence == 4


async def test_mqtt_partial_subscription_failure_unsubscribes(hass, monkeypatch):
    from homeassistant.components import mqtt
    hass.config.components.add("mqtt")
    removed = []
    async def subscribe(hass, topic, callback, qos=0):
        if topic.endswith("availability"):
            raise RuntimeError("private broker address must not escape")
        return lambda: removed.append(topic)
    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "is_connected", lambda hass: True)
    source = {"id": "mqtt_remote", "type": "mqtt", "topic": "remote/action", "availability_topic": "remote/availability",
              "buttons": [{"id": "top", "label": "Top"}], "mappings": [mapping("top", {"action": "single"})]}
    entity = await add(hass, [source])
    assert removed == ["remote/action"]
    assert wire(hass, entity)["bridge_sources"][0]["error"] == "mqtt_subscription_failed"
    assert "private" not in json.dumps(wire(hass, entity))


@pytest.mark.parametrize("source", [
    bus(filters={}), bus(event_type="state_changed"), bus(mappings=[mapping("top", {})]),
    bus(mappings=[mapping("top", {"action": "click"}), mapping("top", {"action": "click", "extra": 1})]),
    bus(mappings=[mapping("missing", {"action": "click"})]), bus(filters={"bad[0]": 1}),
    bus(inactivity_timeout=float("nan")), bus(inactivity_timeout=True), zwave(device_id=None),
    zwave(buttons=[{"id": "top", "label": "Top", "scene": True}]),
    {"id": "mqtt", "type": "mqtt", "topic": "remote/#", "buttons": [{"id": "top", "label": "Top"}], "mappings": [mapping("top", {"action": "click"})]},
])
def test_invalid_source_configuration(source):
    with pytest.raises(ValueError):
        NativeControlConfig.parse({"unique_id": "test", "name": "Test", "sources": [source]})


@pytest.mark.parametrize("sources", [
    [bus(), bus(id="other", buttons=[{"id": "bottom", "label": "Bottom"}], mappings=[mapping("bottom", {"action": "click"})])],
    [{"id": "one", "type": "native_event", "source_entity_id": "event.same", "actions": {"up": {"kind": "press", "press_count": 1}}},
     {"id": "two", "type": "native_event", "source_entity_id": "event.same", "button_id": "bottom", "actions": {"down": {"kind": "press", "press_count": 1}}}],
])
def test_duplicate_physical_source_rejected(sources):
    with pytest.raises(ValueError):
        NativeControlConfig.parse({"unique_id": "test", "name": "Test", "sources": sources})


def test_missing_null_and_bool_filter_semantics():
    assert matches({"args": [None, True, 1]}, {"args.0": None, "args.1": True, "args.2": 1})
    assert not matches({}, {"missing": None})
    assert not matches({"value": 1}, {"value": True})
    assert not matches({"value": True}, {"value": 1})


async def test_bus_and_mqtt_subscription_never_invent_physical_availability(hass, mqtt_boundary):
    from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
    source = {"id": "mqtt_remote", "type": "mqtt", "topic": "remote/action", "buttons": [{"id": "bottom", "label": "Bottom"}],
              "mappings": [mapping("bottom", {"action": "click"})]}
    entity = await add(hass, [bus(), source])
    assert all(row["ready"] and row["availability"] == "unknown" for row in wire(hass, entity)["bridge_sources"])
    await fire(hass, "vendor_button", {"serial": "explicit-device", "action": "click"})
    assert all(row["availability"] == "unknown" for row in wire(hass, entity)["bridge_sources"])
    action = mqtt_boundary[0][source["topic"]]
    action(message(source["topic"], "click"))
    assert wire(hass, entity)["gesture_sequence"] == 2
    assert all(row["availability"] == "unknown" for row in wire(hass, entity)["bridge_sources"])
    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, False)
    await hass.async_block_till_done()
    row = wire(hass, entity)["bridge_sources"][1]
    assert row["ready"] is False and row["availability"] == "unavailable"
    action(message(source["topic"], "click"))
    assert wire(hass, entity)["gesture_sequence"] == 2
    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, True)
    await hass.async_block_till_done()
    row = wire(hass, entity)["bridge_sources"][1]
    assert row["ready"] is True and row["availability"] == "unknown"
    action(message(source["topic"], "click"))
    assert wire(hass, entity)["gesture_sequence"] == 3


async def test_missing_mqtt_does_not_block_another_live_source(hass):
    source = {"id": "mqtt_remote", "type": "mqtt", "topic": "remote/action", "buttons": [{"id": "bottom", "label": "Bottom"}],
              "mappings": [mapping("bottom", {"action": "click"})]}
    entity = await add(hass, [bus(), source])
    row = wire(hass, entity)["bridge_sources"][1]
    assert row["ready"] is False and row["error"] == "mqtt_not_configured"
    await fire(hass, "vendor_button", {"serial": "explicit-device", "action": "click"})
    assert wire(hass, entity)["gesture_sequence"] == 1
    assert wire(hass, entity)["button_id"] == "top"


@pytest.mark.parametrize("change", [{"type": []}, {"source_entity_id": "event.wrong"}, {"device_id": "wrong"},
                                    {"filters": {"action": "other"}}, {"buttons": [{"id": "top", "label": "Top", "scene": 1}]}])
def test_malformed_or_contradictory_source_fields_rejected(change):
    with pytest.raises(ValueError):
        NativeControlConfig.parse({"unique_id": "test", "name": "Test", "sources": [bus(**change)]})


async def test_disjoint_rules_on_same_bus_channel_do_not_collide(hass):
    second = bus(id="second", buttons=[{"id": "bottom", "label": "Bottom"}],
                 mappings=[mapping("bottom", {"action": "triple"}, count=3)])
    entity = await add(hass, [bus(), second])
    for sequence, action, bid in ((1, "click", "top"), (2, "triple", "bottom")):
        await fire(hass, "vendor_button", {"serial": "explicit-device", "action": action})
        attrs = wire(hass, entity)
        assert attrs["gesture_sequence"] == sequence
        assert attrs["button_id"] == bid


async def test_reload_preserves_control_and_button_id_but_never_replays(hass):
    entity = await add(hass, [bus()])
    await fire(hass, "vendor_button", {"serial": "explicit-device", "action": "click"})
    first = wire(hass, entity)
    old_id = entity.entity_id
    await entity.async_remove()
    entity = await add(hass, [bus()])
    assert entity.entity_id == old_id
    current = wire(hass, entity)
    assert current["bridge_control_id"] == first["bridge_control_id"]
    assert current["bridge_buttons"] == first["bridge_buttons"]
    assert current["bridge_session_id"] != first["bridge_session_id"]
    assert current["bridge_update_kind"] == "snapshot"
    await fire(hass, "vendor_button", {"serial": "explicit-device", "action": "click"})
    current = wire(hass, entity)
    assert current["gesture_sequence"] == 1
    assert current["gesture_session_id"] != first["gesture_session_id"]


@pytest.mark.parametrize("form", ["legacy", "buttons", "native", "shared"])
@pytest.mark.parametrize("persist_registry", [False, True])
async def test_native_registry_rename_rebinds_without_replay_and_detaches(hass, form, persist_registry):
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)
    row = registry.async_get_or_create("event", "fixture_source", "physical-paddle", suggested_object_id="original_paddle")
    original = row.entity_id
    hass.states.async_set(original, "unknown")
    ref = {"source_registry_id": row.id} if persist_registry else {}
    actions = {"double": {"kind": "press", "press_count": 2}}
    config = {"unique_id": "rename-fixture", "name": "Rename Fixture"}
    if form == "legacy":
        config.update(source_entity_id=original, actions=actions, **ref)
    elif form == "buttons":
        config["buttons"] = [{"id": "top", "label": "Top", "source_entity_id": original, "actions": actions, **ref}]
    elif form == "native":
        config["sources"] = [{"id": "paddle", "type": "native_event", "source_entity_id": original, "actions": actions, **ref}]
    else:
        config["sources"] = [{"id": "paddle", "type": "shared_native", "source_entity_id": original,
                              "buttons": [{"id": "top", "label": "Top"}],
                              "mappings": [mapping("top", {"event_type": "double"}, count=2)], **ref}]
    parsed = NativeControlConfig.parse(config)
    entity = GestureBridgeEvent(parsed)
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([entity])
    hass.states.async_set(original, dt_util.utcnow().isoformat(), {"event_type": "double"})
    await hass.async_block_till_done()
    first = wire(hass, entity)
    assert first["gesture_sequence"] == 1
    renamed = "event.renamed_paddle"
    # HA applies the registry rename before the new state is available. The
    # registry callback sees this copied last occurrence as baseline metadata.
    registry.async_update_entity(original, new_entity_id=renamed)
    hass.states.async_set(renamed, dt_util.utcnow().isoformat(), {"event_type": "double"})
    await hass.async_block_till_done()
    attrs = wire(hass, entity)
    assert attrs["bridge_update_kind"] == "snapshot"
    assert attrs["gesture_sequence"] == 1
    assert attrs["bridge_buttons"] == first["bridge_buttons"]
    hass.states.async_set(original, dt_util.utcnow().isoformat(), {"event_type": "double"})
    await hass.async_block_till_done()
    assert wire(hass, entity)["gesture_sequence"] == 1
    hass.states.async_set(renamed, dt_util.utcnow().isoformat(), {"event_type": "double"})
    await hass.async_block_till_done()
    assert wire(hass, entity)["gesture_sequence"] == 2
    assert wire(hass, entity)["bridge_control_id"] == first["bridge_control_id"]
    bindings = entity._native_bindings + [adapter.native_binding for adapter in entity._adapters]
    assert all(binding.registry_id == row.id and binding.entity_id == renamed for binding in bindings)
    await entity.async_remove()
    assert all(binding._state_unsub is None and binding._registry_unsub is None for binding in bindings)
    registry.async_update_entity(renamed, new_entity_id="event.renamed_again")
    hass.states.async_set(renamed, dt_util.utcnow().isoformat(), {"event_type": "double"})
    await hass.async_block_till_done()
    assert entity._sequence == 2
    if persist_registry:
        # Stored configuration still has the obsolete entity name, but the
        # authoritative registry UUID resolves the latest name after reload.
        entity = GestureBridgeEvent(NativeControlConfig.parse(config))
        await component.async_add_entities([entity])
        await hass.async_block_till_done()
        assert wire(hass, entity)["bridge_update_kind"] == "snapshot"
        hass.states.async_set("event.renamed_again", "unknown")
        await hass.async_block_till_done()
        hass.states.async_set("event.renamed_again", dt_util.utcnow().isoformat(), {"event_type": "double"})
        await hass.async_block_till_done()
        assert wire(hass, entity)["gesture_sequence"] == 1
        assert wire(hass, entity)["bridge_control_id"] == first["bridge_control_id"]


@pytest.mark.parametrize("canonical", [False, True])
async def test_removed_native_registry_identity_never_attaches_replacement(hass, canonical):
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)
    row = registry.async_get_or_create("event", "fixture_source", "original-device", suggested_object_id="paddle")
    source = row.entity_id
    hass.states.async_set(source, "unknown")
    config = {"unique_id": "identity-fixture", "name": "Identity Fixture", "source_entity_id": source,
              "source_registry_id": row.id, "actions": {"click": {"kind": "press", "press_count": 1}}}
    if canonical:
        config = {"unique_id": config["unique_id"], "name": config["name"], "sources": [
            {"id": "paddle", "type": "native_event", "source_entity_id": source, "source_registry_id": row.id,
             "actions": config["actions"]}]}
    entity = GestureBridgeEvent(NativeControlConfig.parse(config))
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([entity])
    registry.async_remove(source)
    await hass.async_block_till_done()
    assert wire(hass, entity)["bridge_sources"][0]["ready"] is False
    hass.states.async_remove(source)
    replacement = registry.async_get_or_create("event", "fixture_source", "replacement-device", suggested_object_id="paddle")
    assert replacement.entity_id == source and replacement.id != row.id
    hass.states.async_set(source, dt_util.utcnow().isoformat(), {"event_type": "click"})
    await hass.async_block_till_done()
    assert "gesture_sequence" not in wire(hass, entity)
    assert wire(hass, entity)["bridge_sources"][0]["ready"] is False


@pytest.mark.parametrize("value", ["", None, 12, [], "event.not_a_uuid"])
def test_invalid_native_registry_identity_rejected(value):
    config = {"unique_id": "identity-fixture", "name": "Identity Fixture", "source_entity_id": "event.input",
              "source_registry_id": value, "actions": {"click": {"kind": "press", "press_count": 1}}}
    with pytest.raises(ValueError):
        NativeControlConfig.parse(config)
