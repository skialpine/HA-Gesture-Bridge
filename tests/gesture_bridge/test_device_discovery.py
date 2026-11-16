"""Actual HA registries and core device-trigger dispatch; radio edges simulated."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import logging
from types import MappingProxyType, SimpleNamespace

from homeassistant.components import device_automation
from homeassistant.components import mqtt
from homeassistant.components.mqtt.device_trigger import Trigger
from homeassistant.components.mqtt.models import DATA_MQTT, ReceiveMessage
from homeassistant.components.device_automation import helpers as automation_helpers, trigger as device_trigger
from homeassistant.config_entries import ConfigEntry, SOURCE_USER
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig, SourceConfig
from custom_components.ha_gesture_bridge.device_discovery import discover_device
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent
from custom_components.ha_gesture_bridge.sources import SourceAdapter
from tools.validate_gesture_contract import validate_state


async def device(hass, name="Test Switch"):
    entry = ConfigEntry(domain=DOMAIN, title="HA Gesture Bridge", data={}, options={}, version=1, minor_version=1,
        source=SOURCE_USER, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[])
    await hass.config_entries.async_add(entry)
    return dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "test-switch")}, name=name)


def native(hass, device_id, label, actions, unique=None, *, platform="fixture_remote"):
    item = er.async_get(hass).async_get_or_create("event", platform, unique or label,
        device_id=device_id, original_name=label)
    hass.states.async_set(item.entity_id, "unknown", {"event_types": actions, "event_type": None})
    return item


def provider(hass, monkeypatch, triggers):
    calls, callbacks, detached = [], [], []

    async def get_triggers(hass, device_id):
        return deepcopy(triggers)

    async def attach(hass, config, action, info):
        calls.append((deepcopy(config), deepcopy(info)))
        callbacks.append(action)
        return lambda: detached.append(config["type"])

    platform = SimpleNamespace(TRIGGER_SCHEMA=device_trigger.TRIGGER_SCHEMA,
        async_get_triggers=get_triggers, async_attach_trigger=attach)

    async def get_platform(_hass, domain, _kind):
        if domain in {DOMAIN, "fixture_remote", "zha", "mqtt", "zwave_js"}:
            return platform
        raise device_automation.InvalidDeviceAutomationConfig

    monkeypatch.setattr(device_automation, "async_get_device_automation_platform", get_platform)
    monkeypatch.setattr(automation_helpers, "async_get_device_automation_platform", get_platform)
    monkeypatch.setattr(device_trigger, "async_get_device_automation_platform", get_platform)
    return calls, callbacks, detached, platform


def trigger(device_id, kind, subtype="button_1", domain="fixture_remote", **extra):
    return {"platform": "device", "device_id": device_id, "domain": domain, "type": kind, "subtype": subtype, **extra}


async def test_native_device_detects_only_reported_gestures_and_registry_roles(hass, monkeypatch):
    row = await device(hass, "Office")
    upper = native(hass, row.id, "Upper Paddle", ["KeyPressed", "KeyPressed2x", "KeyHeldDown", "KeyReleased"])
    native(hass, row.id, "Lower Paddle", ["KeyPressed3x", "vendor_special"])
    called = False

    async def forbidden(*args):
        nonlocal called
        called = True
        raise AssertionError("Native detection must not also attach device triggers")

    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", forbidden)
    discovered = await discover_device(hass, row.id)
    assert discovered.device_name == "Office" and len(discovered.buttons) == 2
    assert not called
    by_role = {button.suggested_role: button for button in discovered.buttons}
    assert set(by_role) == {"top", "bottom"}
    assert {g.id for g in by_role["top"].gestures} == {"press_1", "press_2", "hold_start", "release"}
    assert {g.kind for g in by_role["bottom"].gestures} == {"press", "custom"}
    assert "press_1" not in {g.id for g in by_role["bottom"].gestures}
    roles = {button.button_id: button.suggested_role for button in discovered.buttons}
    config = discovered.build_config(roles, unique_id="office", name="Office Gestures")
    parsed = NativeControlConfig.parse(config)
    assert {button.id for button in parsed.inputs} == {"top", "bottom"}
    assert config["source_device_id"] == row.id and config["source_button_roles"] == roles
    assert GestureBridgeEvent(parsed).extra_state_attributes["bridge_source_device_id"] == row.id
    assert all(source.type == "native_event" for source in parsed.runtime_sources())
    er.async_get(hass).async_update_entity(upper.entity_id, new_entity_id="event.renamed_upper")
    hass.states.async_set("event.renamed_upper", "unknown", {"event_types": ["KeyPressed"]})
    again = await discover_device(hass, row.id)
    assert next(button for button in again.buttons if button.source_registry_id == upper.id).button_id == by_role["top"].button_id


@pytest.mark.parametrize("profile", ["dimmer", "tap", "rotary", "other_provider"])
async def test_native_hue_vocabulary_discovery_and_publication(hass, profile):
    # Independent vocabulary fixture from HA 2026.11.1 hue/const.py's
    # DEFAULT_BUTTON_EVENT_TYPES, DEFAULT_ROTARY_EVENT_TYPES and Tap override.
    # Avoid importing Hue's optional radio/client dependencies into bridge tests.
    event_types = (["initial_press"] if profile == "tap" else
                   ["start", "repeat"] if profile == "rotary" else
                   ["initial_press", "repeat", "short_release", "long_press", "long_release"])
    row = await device(hass, "Hue Control")
    source = native(hass, row.id, "Upper Button", event_types,
                    platform="fixture_remote" if profile == "other_provider" else "hue")
    discovered = await discover_device(hass, row.id)
    button = discovered.buttons[0]
    config = discovered.build_config({button.button_id: "top"}, unique_id="hue-control", name="Hue Gestures")
    parsed = NativeControlConfig.parse(config)
    actions = button.actions
    if profile == "dimmer":
        assert actions["initial_press"]["kind"] == "custom"  # Down alone must not click before a hold.
        assert actions["short_release"]["kind"] == "press"
        assert actions["short_release"]["press_count"] == 1
        assert actions["repeat"]["kind"] == "hold_repeat"
        expected = [("initial_press", "custom"), ("short_release", "press"),
                    ("initial_press", "custom"), ("long_press", "hold_start"),
                    ("repeat", "hold_repeat"), ("repeat", "hold_repeat"), ("long_release", "release")]
        assert {gesture.id for gesture in parsed.inputs[0].actions.values()} >= {"press_1", "hold_start", "hold_repeat", "release"}
    elif profile == "tap":
        expected = [("initial_press", "press"), ("initial_press", "press")]
        assert {gesture.id for gesture in parsed.inputs[0].actions.values()} == {"press_1"}
    elif profile == "rotary":
        expected = [("start", "custom"), ("repeat", "custom")]
        assert all(action["kind"] == "custom" for action in actions.values())
    else:
        # A provider sharing an action's spelling does not inherit Hue semantics.
        expected = [("initial_press", "custom"), ("short_release", "release"), ("repeat", "custom")]

    aggregate = GestureBridgeEvent(parsed)
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([aggregate]); await hass.async_block_till_done()
    assert "gesture_sequence" not in hass.states.get(aggregate.entity_id).attributes
    base = dt_util.utcnow()
    for index, (action, kind) in enumerate(expected, 1):
        hass.states.async_set(source.entity_id, (base + timedelta(milliseconds=index)).isoformat(),
                              {"event_types": event_types, "event_type": action})
        await hass.async_block_till_done()
        state = hass.states.get(aggregate.entity_id)
        validate_state(json.loads(json.dumps(state.as_dict(), default=str)))
        assert state.attributes["gesture_sequence"] == index
        assert state.attributes["event_type"] == kind
        assert state.attributes["button_id"] == "top"
        if kind == "press":
            assert state.attributes["gesture_id"] == "press_1" and state.attributes["press_count"] == 1
        else:
            assert "press_count" not in state.attributes


async def test_disabled_and_aggregate_sources_and_load_entities_are_not_inputs(hass, monkeypatch):
    row = await device(hass)
    disabled = native(hass, row.id, "Disabled", ["single"])
    er.async_get(hass).async_update_entity(disabled.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    aggregate = native(hass, row.id, "Aggregate", ["single"])
    hass.states.async_set(aggregate.entity_id, "unknown", {"event_types": ["single"], "bridge_contract_version": "1.0"})
    provider(hass, monkeypatch, [trigger(row.id, "turned_on", domain="switch"),
        trigger(row.id, "pressed", domain="button"), trigger(row.id, "state.node_status", domain="zwave_js"),
        trigger(row.id, "event.value_updated.config_parameter", domain="zwave_js")])
    assert (await discover_device(hass, row.id)).buttons == ()


async def test_standard_device_trigger_capabilities_and_unknown_physical_action(hass, monkeypatch):
    row = await device(hass)
    declared = [trigger(row.id, name) for name in ("remote_button_short_press", "remote_button_double_press",
        "remote_button_triple_press", "remote_button_long_press", "remote_button_long_release", "remote_button_rotated")]
    calls, callbacks, detached, _ = provider(hass, monkeypatch, declared)
    discovered = await discover_device(hass, row.id)
    assert len(discovered.buttons) == 1
    button = discovered.buttons[0]
    assert button.suggested_role == "toggle"
    assert {g.id for g in button.gestures} >= {"press_1", "press_2", "press_3", "hold_start", "release"}
    assert sum(g.kind == "custom" for g in button.gestures) == 1
    declared.reverse()
    assert (await discover_device(hass, row.id)).buttons == discovered.buttons
    config = discovered.build_config({button.button_id: "toggle"}, unique_id="device-control", name="Device Gestures")
    aggregate = GestureBridgeEvent(NativeControlConfig.parse(config))
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([aggregate]); await hass.async_block_till_done()
    assert len(calls) == len(declared)
    assert {call[0]["type"] for call in calls} == {item["type"] for item in declared}
    double_callback = callbacks[next(index for index, call in enumerate(calls) if call[0]["type"] == "remote_button_double_press")]
    assert "gesture_sequence" not in hass.states.get(aggregate.entity_id).attributes
    validate_state(json.loads(json.dumps(hass.states.get(aggregate.entity_id).as_dict(), default=str)))
    await double_callback({"trigger": {}})
    await hass.async_block_till_done()
    first = hass.states.get(aggregate.entity_id).attributes
    validate_state(json.loads(json.dumps(hass.states.get(aggregate.entity_id).as_dict(), default=str)))
    assert first["gesture_id"] == "press_2" and first["button_id"] == "toggle" and first["gesture_sequence"] == 1
    await double_callback({"trigger": {}}); await hass.async_block_till_done()
    assert hass.states.get(aggregate.entity_id).attributes["gesture_sequence"] == 2
    await aggregate.async_remove(); await hass.async_block_till_done()
    assert len(detached) == len(declared)
    await double_callback({"trigger": {}})  # Closed attachment cannot publish history.


async def test_generic_central_scene_does_not_invent_counts(hass, monkeypatch):
    row = await device(hass)
    provider(hass, monkeypatch, [trigger(row.id, "event.value_notification.central_scene", "Endpoint0Scene001",
        domain="zwave_js", command_class=91, property="scene", property_key="001", endpoint=0)])
    result = await discover_device(hass, row.id)
    assert len(result.buttons) == 1
    assert all(gesture.kind == "custom" and gesture.press_count is None for gesture in result.buttons[0].gestures)


async def test_roles_require_complete_unique_assignments_and_limits(hass, monkeypatch):
    row = await device(hass)
    native(hass, row.id, "One", ["single"]); native(hass, row.id, "Two", ["double"])
    result = await discover_device(hass, row.id)
    with pytest.raises(ValueError):
        result.build_config({}, unique_id="test", name="Test")
    with pytest.raises(ValueError):
        result.build_config({button.button_id: "top" for button in result.buttons}, unique_id="test", name="Test")
    for index in range(31):
        native(hass, row.id, f"Other {index}", ["single"])
    with pytest.raises(ValueError, match="too many physical"):
        await discover_device(hass, row.id)


async def test_trigger_partial_failure_and_close_during_attach_clean_up(hass, monkeypatch):
    row = await device(hass)
    calls, callbacks, detached, platform = provider(hass, monkeypatch,
        [trigger(row.id, "remote_button_short_press"), trigger(row.id, "remote_button_double_press")])
    discovered = await discover_device(hass, row.id)
    config = SourceConfig.parse(discovered.build_config({discovered.buttons[0].button_id: "toggle"},
        unique_id="test", name="Test")["sources"][0])
    original = platform.async_attach_trigger

    async def fail(hass, config, action, info):
        if calls:
            raise RuntimeError("private integration details")
        return await original(hass, config, action, info)

    platform.async_attach_trigger = fail
    adapter = SourceAdapter(hass, config, lambda *args: None, lambda *args: None, lambda *args: None, lambda *args: None)
    await adapter.async_start()
    assert not adapter.ready and adapter.error == "device_trigger_unavailable"
    assert len(detached) == 1
    gate = asyncio.Event()

    async def deferred(hass, config, action, info):
        await gate.wait()
        return await original(hass, config, action, info)

    platform.async_attach_trigger = deferred
    adapter = SourceAdapter(hass, config, lambda *args: None, lambda *args: None, lambda *args: None, lambda *args: None)
    task = asyncio.create_task(adapter.async_start()); await asyncio.sleep(0)
    adapter.close(); gate.set(); await task
    assert not adapter.ready
    assert len(detached) == 2


async def mqtt_fixture(hass, monkeypatch):
    row = await device(hass)
    declarations = [trigger(row.id, "action", action, domain="mqtt") for action in ("single", "double")]
    provider(hass, monkeypatch, declarations)
    targets = {action: Trigger(device_id=row.id, hass=hass, payload=action, qos=0, subtype=action,
        topic="zigbee/fixture/action", type="action", value_template=None) for action in ("single", "double")}
    hass.data[DATA_MQTT] = SimpleNamespace(device_triggers=targets)
    hass.config.components.add("mqtt")
    received, connected = [], []

    async def subscribe(_hass, topic, callback, qos=0):
        received.append((topic, callback))
        return lambda: None

    monkeypatch.setattr(mqtt, "is_connected", lambda _hass: True)
    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_subscribe_connection_status", lambda _hass, cb: connected.append(cb) or (lambda: None))
    return row, targets, received, connected


async def test_mqtt_device_discovery_retains_provenance_after_start_and_reconnect(hass, monkeypatch):
    row, targets, received, connected = await mqtt_fixture(hass, monkeypatch)
    discovered = await discover_device(hass, row.id)
    assert len(discovered.buttons) == 1
    button = discovered.buttons[0]
    assert {gesture.id for gesture in button.gestures} == {"press_1", "press_2"}
    saved = discovered.build_config({button.button_id: "toggle"}, unique_id="mqtt-control", name="Remote")
    assert saved["sources"][0]["type"] == "mqtt" and saved["sources"][0]["raw_payload"] is True
    config = SourceConfig.parse(saved["sources"][0])
    emitted = []
    adapter = SourceAdapter(hass, config, lambda _adapter, button, gesture: emitted.append((button, gesture.id)),
        lambda *_: None, lambda *_: None, lambda *_: None)
    await adapter.async_start()
    assert adapter.ready and len(received) == 1
    callback = received[0][1]
    callback(ReceiveMessage("zigbee/fixture/action", "single", 0, True, "zigbee/fixture/action", 0.0))
    assert emitted == []
    callback(ReceiveMessage("zigbee/fixture/action", "single", 0, False, "zigbee/fixture/action", 0.0))
    assert emitted == [("toggle", "press_1")]  # First live event is never sacrificed.
    connected[0](False); connected[0](True)
    callback(ReceiveMessage("zigbee/fixture/action", "double", 0, True, "zigbee/fixture/action", 0.0))
    assert len(emitted) == 1
    callback(ReceiveMessage("zigbee/fixture/action", "double", 0, False, "zigbee/fixture/action", 0.0))
    assert emitted[-1] == ("toggle", "press_2")
    targets["single"].topic = "zigbee/renamed/action"
    callback(ReceiveMessage("zigbee/fixture/action", "single", 0, False, "zigbee/fixture/action", 0.0))
    assert len(emitted) == 2 and not adapter.ready and adapter.error == "mqtt_device_changed"
    targets["single"].topic = "zigbee/fixture/action"
    callback(ReceiveMessage("zigbee/fixture/action", "single", 0, False, "zigbee/fixture/action", 0.0))
    assert len(emitted) == 3 and adapter.ready and adapter.availability == "unknown"
    adapter.close()


async def test_mqtt_templates_are_explicitly_unsupported_and_deleted_device_revokes(hass, monkeypatch):
    row, targets, received, connected = await mqtt_fixture(hass, monkeypatch)
    targets["single"].value_template = "{{ value_json.action }}"
    with pytest.raises(ValueError, match="value templates"):
        await discover_device(hass, row.id)
    targets["single"].value_template = None
    discovered = await discover_device(hass, row.id)
    saved = discovered.build_config({discovered.buttons[0].button_id: "toggle"},
        unique_id="mqtt-control", name="Remote")["sources"][0]
    saved["availability_topic"] = "zigbee/fixture/availability"
    config = SourceConfig.parse(saved)
    adapter = SourceAdapter(hass, config, lambda *_: pytest.fail("Deleted device published a gesture"),
        lambda *_: None, lambda *_: None, lambda *_: None)
    await adapter.async_start()
    received[1][1](ReceiveMessage("zigbee/fixture/availability", "online", 0, True, "zigbee/fixture/availability", 0.0))
    assert adapter.ready
    dr.async_get(hass).async_remove_device(row.id)
    await hass.async_block_till_done()
    assert not adapter.ready
    received[0][1](ReceiveMessage("zigbee/fixture/action", "single", 0, False, "zigbee/fixture/action", 0.0))
    received[1][1](ReceiveMessage("zigbee/fixture/availability", "online", 0, True, "zigbee/fixture/availability", 0.0))
    connected[0](True)
    assert not adapter.ready
    adapter.close()


async def test_device_trigger_schema_rejects_duplicate_physical_subscription_and_unsafe_mqtt(hass, monkeypatch):
    row = await device(hass)
    provider(hass, monkeypatch, [trigger(row.id, "remote_button_short_press")])
    discovered = await discover_device(hass, row.id)
    config = discovered.build_config({discovered.buttons[0].button_id: "toggle"}, unique_id="test", name="Test")
    source = config["sources"][0]
    duplicate = deepcopy(source["triggers"][0]); duplicate["id"] = "duplicate"
    source["triggers"].append(duplicate)
    source["mappings"].append({"button_id": "toggle", "match": {"trigger_id": "duplicate"},
                               "gesture": {"kind": "press", "press_count": 1}})
    with pytest.raises(ValueError, match="more than once"):
        SourceConfig.parse(source)
    source["triggers"].pop(); source["mappings"].pop()
    other = deepcopy(source); other["id"] = "other"; other["buttons"][0]["id"] = "other"
    other["mappings"][0]["button_id"] = "other"; other["triggers"][0]["id"] = "other_trigger"
    other["mappings"][0]["match"]["trigger_id"] = "other_trigger"
    config["sources"].append(other)
    with pytest.raises(ValueError, match="more than once"):
        NativeControlConfig.parse(config)
    source["triggers"][0]["config"]["domain"] = "mqtt"
    with pytest.raises(ValueError):
        SourceConfig.parse(source)


async def test_expected_provider_unavailability_stays_a_discovery_error(hass, monkeypatch):
    row = await device(hass)

    async def unavailable(*_args):
        raise HomeAssistantError("private connection details")

    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", unavailable)
    with pytest.raises(ValueError, match="gesture provider is unavailable"):
        await discover_device(hass, row.id)


async def test_mqtt_multitopic_button_is_rejected_before_review(hass, monkeypatch):
    row, targets, received, connected = await mqtt_fixture(hass, monkeypatch)
    targets["double"].topic = "zigbee/fixture/double"
    with pytest.raises(ValueError, match="multiple topics"):
        await discover_device(hass, row.id)


@pytest.mark.parametrize("domain,kind,extra", [
    ("switch", "turned_on", {"entity_id": "switch.load"}),
    ("light", "turned_off", {"entity_id": "light.load"}),
    ("button", "pressed", {"entity_id": "button.ping"}),
    ("zwave_js", "event.value_updated.config_parameter", {"command_class": 112}),
    ("zwave_js", "state.node_status", {}),
])
async def test_manual_load_config_ping_triggers_cannot_become_gestures(hass, monkeypatch, domain, kind, extra):
    row = await device(hass)
    calls, callbacks, detached, _ = provider(hass, monkeypatch, [trigger(row.id, "remote_button_short_press")])
    discovery = await discover_device(hass, row.id)
    source = discovery.build_config({discovery.buttons[0].button_id: "toggle"}, unique_id="test", name="Test")["sources"][0]
    valid = SourceConfig.parse(source)
    unsafe = trigger(row.id, kind, domain=domain, **extra)
    source["triggers"][0]["config"] = unsafe
    with pytest.raises(ValueError, match="physical button"):
        SourceConfig.parse(source)
    # Defense also applies to manually constructed dataclasses that bypass parse.
    unsafe_config = replace(valid, device_triggers=(replace(valid.device_triggers[0], config=unsafe),))
    adapter = SourceAdapter(hass, unsafe_config, lambda *_: pytest.fail("Load state published as a physical input"),
        lambda *_: None, lambda *_: None, lambda *_: None)
    await adapter.async_start()
    assert not adapter.ready and adapter.error == "device_trigger_unavailable" and calls == []
