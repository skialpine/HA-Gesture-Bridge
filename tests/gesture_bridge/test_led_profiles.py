"""Actual aggregate/service boundary with simulated Z-Wave hardware replies."""

import asyncio
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

from homeassistant.config_entries import ConfigEntry, SOURCE_USER
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
import voluptuous as vol
from zwave_js_server.event import EventBase
from zwave_js_server.model.value import ConfigurationValue

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge import zwave_indicator as indicator
from custom_components.ha_gesture_bridge.led_profiles import PROFILES, profile_for_device
from custom_components.ha_gesture_bridge.zwave_parameters import discover_parameters
from tools.generate_led_fixture import setup_fixture, state_fixture


class ParameterNode(EventBase):
    """Real value objects, raw CC transport and independent physical state."""

    def __init__(self, profile, *, feedback=True):
        super().__init__()
        self.node_id = 7
        self.manufacturer_id, self.product_type, self.product_id = (
            profile.manufacturer_id, *profile.fingerprints[0])
        self.ready = True
        self.status = 1
        self.client = SimpleNamespace(connected=True)
        self.values, self.physical, self.calls = {}, {}, []
        self.feedback, self.gate, self.read_gate = feedback, None, None
        for channel in profile.channels:
            settings = [(channel.mode, 0)]
            if channel.colors:
                settings.append((channel.colors[0].schema, 1))
            if channel.brightness:
                settings.append((channel.brightness[0].schema, 1))
            for schema, current in settings:
                self.add_parameter(schema, current)
        self.endpoints = {0: SimpleNamespace(async_invoke_cc_api=self.invoke)}

    def add_parameter(self, schema, current):
        value = ConfigurationValue(self, {"commandClass": 112, "commandClassName": "Configuration",
            "endpoint": 0, "property": schema.parameter, "propertyName": schema.label,
            "ccVersion": 4, "value": current, "metadata": {"type": "number", "readable": True,
                "writeable": True, "label": schema.label, "min": min(key for key, _ in schema.states),
                "max": max(key for key, _ in schema.states), "valueSize": 1,
                "states": {str(key): label for key, label in schema.states}, "isFromConfig": True}})
        self.values[value.value_id] = value
        self.physical[schema.parameter] = current

    async def invoke(self, cc, method, *args, wait_for_result):
        assert cc.value == 112 and wait_for_result is True
        assert method in {"get", "set"}
        self.calls.append((cc.value, method, deepcopy(args)))
        if method == "get":
            if self.read_gate is not None and args[0] == 14:
                await self.read_gate.wait()
            return self.physical[args[0]]
        if self.gate is not None:
            await self.gate.wait()
        data = args[0]
        assert set(data) == {"parameter", "value"}
        assert data["parameter"] in self.physical
        if self.feedback:
            self.physical[data["parameter"]] = data["value"]


async def office(hass, monkeypatch, *, profile=None, feedback=True, change=None, configure_source=None):
    profile = profile or next(item for item in PROFILES if item.model == "ZEN76")
    entry = ConfigEntry(domain=DOMAIN, title="HA Gesture Bridge", data={}, options={}, version=1, minor_version=1,
        source=SOURCE_USER, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[])
    await hass.config_entries.async_add(entry)
    row = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("zwave_js", "4070226031-7")}, manufacturer=profile.manufacturer,
        model=profile.model, name="Jeff's Office Switch")
    node = ParameterNode(profile, feedback=feedback)
    if change:
        change(node)
    current = {"node": node}
    def resolve(_hass, device_id):
        return current["node"] if device_id == row.id else None
    monkeypatch.setattr("custom_components.ha_gesture_bridge.zwave_indicator._resolve_node", resolve)
    monkeypatch.setattr("custom_components.ha_gesture_bridge.zwave_parameters._resolve_node", resolve)
    def configure(config):
        config.pop("leds")
        config["source_device_id"] = row.id
        if configure_source:
            configure_source(config, row, node)
    f = await setup_fixture(hass, configure=configure)
    # Z-Wave reads are background tasks to avoid blocking entity setup.
    for _ in range(10):
        await asyncio.sleep(0)
    for parameter in node.physical:
        er.async_get(hass).async_get_or_create("select", "zwave_js", f"4070226031.7-112-0-{parameter}",
            device_id=row.id, config_entry=entry, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    return f, row, node, current


def channel(hass, f):
    return state_fixture(hass, f["aggregate"])["attributes"]["bridge_leds"][0]


async def command(hass, f, **data):
    await hass.services.async_call(DOMAIN, "set_led", {
        "entity_id": f["aggregate"].entity_id, "led_id": "status", **data}, blocking=True)
    await hass.async_block_till_done()


async def test_actual_aggregate_auto_discovers_office_palette_without_enabling_entities(hass, monkeypatch):
    f, row, node, _ = await office(hass, monkeypatch)
    assert f["config"].get("leds") is None
    assert profile_for_device(hass, row.id).model == "ZEN76"
    assert all(method == "get" for _cc, method, _args in node.calls)
    attrs = state_fixture(hass, f["aggregate"])["attributes"]
    assert attrs["bridge_led_version"] == 1
    assert channel(hass, f) == {"id": "status", "label": "Status LED", "button_id": "toggle",
        "available": True, "colors": ["000000", "0000FF", "00FF00", "FF0000", "FFFFFF"],
        "brightness_supported": True, "brightness": 60, "color_supported": True,
        "behaviors": ["follow_load", "follow_load_inverted", "always_on", "always_off"],
        "behavior": "follow_load_inverted"}
    f["paddle"]._trigger_event("KeyPressed")
    f["paddle"].async_write_ha_state()
    await hass.async_block_till_done()
    before = state_fixture(hass, f["aggregate"])
    node.calls.clear()
    await command(hass, f, color="FF0000", brightness=80, behavior="always_on")
    assert [(args[0]["parameter"], args[0]["value"]) for _cc, method, args in node.calls if method == "set"] == [(14, 3), (2, 3), (15, 1)]
    assert channel(hass, f)["color"] == "FF0000" and channel(hass, f)["brightness"] == 60
    after = state_fixture(hass, f["aggregate"])
    assert after["entity_id"] == before["entity_id"]
    for key in ("gesture_sequence", "gesture_id", "gesture_occurred_at", "bridge_session_id", "friendly_name"):
        assert after["attributes"].get(key) == before["attributes"].get(key)
    assert after["attributes"]["bridge_update_kind"] == "snapshot"
    assert all(cc == 112 for cc, _method, _args in node.calls)
    disabled = er.async_entries_for_device(er.async_get(hass), row.id, include_disabled_entities=True)
    assert len(disabled) == 3
    assert all(item.disabled_by is er.RegistryEntryDisabler.INTEGRATION for item in disabled)


@pytest.mark.parametrize("model,color_parameter,brightness_parameter", [
    ("ZEN71", 14, 15), ("ZEN72", 23, 24), ("ZEN76", 14, 15), ("ZEN77", 23, 24)])
async def test_profiles_use_reviewed_product_specific_parameter_numbers(hass, monkeypatch, model, color_parameter, brightness_parameter):
    profile = next(item for item in PROFILES if item.model == model)
    f, _row, node, _ = await office(hass, monkeypatch, profile=profile)
    await command(hass, f, color="0000FF", brightness=30, behavior="always_on")
    writes = [args[0] for _cc, method, args in node.calls if method == "set"]
    assert writes == [{"parameter": color_parameter, "value": 1}, {"parameter": 2, "value": 3},
                      {"parameter": brightness_parameter, "value": 2}]


async def test_readback_wins_over_requested_values_and_off_preserves_saved_color(hass, monkeypatch):
    f, _row, node, _ = await office(hass, monkeypatch, feedback=False)
    await command(hass, f, color="FF0000", brightness=100)
    assert "color" not in channel(hass, f)  # Hardware remains load dependent.
    assert channel(hass, f)["brightness"] == 60
    node.feedback = True
    await command(hass, f, color="000000", behavior="always_off")
    assert channel(hass, f)["color"] == "000000"
    assert node.physical[14] == 1
    assert {args[0]["parameter"] for _cc, method, args in node.calls if method == "set"} <= {2, 14, 15}


@pytest.mark.parametrize("change", [
    lambda node: setattr(node, "manufacturer_id", 0x9999),
    lambda node: setattr(node, "product_id", 0xA007),
    lambda node: node.values["7-112-0-14"].metadata.data.update(states={"0": "White", "1": "Red", "2": "Green", "3": "Blue"}),
    lambda node: node.values["7-112-0-14"].metadata.data.update(valueSize=2),
    lambda node: node.values["7-112-0-2"].metadata.data.update(label="Relay Control"),
    lambda node: node.values["7-112-0-15"].metadata.data.update(writeable=False),
    lambda node: node.values.pop("7-112-0-14"),
])
async def test_unreviewed_hardware_or_schema_rejects_all_writes(hass, monkeypatch, change):
    f, _row, node, _ = await office(hass, monkeypatch, change=change)
    assert not f["aggregate"]._leds.snapshot()
    with pytest.raises(ServiceValidationError):
        await command(hass, f, color="FFFFFF")
    assert node.calls == []


@pytest.mark.parametrize("data", [{"color": "111111"}, {"brightness": 101}, {"brightness": True}, {}])
async def test_rejected_action_has_no_parameter_side_effects(hass, monkeypatch, data):
    f, _row, node, _ = await office(hass, monkeypatch)
    node.calls.clear()
    with pytest.raises((ServiceValidationError, ValueError, vol.Invalid)):
        await command(hass, f, **data)
    assert node.calls == []


@pytest.mark.parametrize("interruption", ["unload", "schema"])
async def test_schema_change_and_unload_interrupt_remaining_writes(hass, monkeypatch, interruption):
    f, _row, node, _ = await office(hass, monkeypatch)
    node.calls.clear()
    node.gate = asyncio.Event()
    task = asyncio.create_task(command(hass, f, color="FF0000", brightness=30))
    for _ in range(100):
        if node.calls or task.done():
            break
        await asyncio.sleep(0)
    assert node.calls
    if interruption == "unload":
        await f["aggregate"].async_remove()
    else:
        node.values["7-112-0-15"].metadata.data["label"] = "Unexpected parameter"
    node.gate.set()
    with pytest.raises(ServiceValidationError):
        await task
    assert [(cc, method) for cc, method, _args in node.calls] == [(112, "set")]


async def test_transport_rebind_requires_new_confirmed_read_without_startup_writes(hass, monkeypatch):
    f, row, node, current = await office(hass, monkeypatch)
    await command(hass, f, color="FF0000")
    controller = f["aggregate"]._leds
    current["node"] = None
    controller._refresh()
    assert not channel(hass, f)["available"] and "color" not in channel(hass, f)
    replacement = ParameterNode(profile_for_device(hass, row.id))
    current["node"] = replacement
    controller._refresh()
    assert "color" not in channel(hass, f)
    for _ in range(10):
        await asyncio.sleep(0)
    assert channel(hass, f)["brightness"] == 60 and "color" not in channel(hass, f)
    assert all(method == "get" for _cc, method, _args in replacement.calls)


async def test_node_replaced_during_read_cannot_publish_old_partial_feedback(hass, monkeypatch):
    f, row, node, current = await office(hass, monkeypatch)
    controller = f["aggregate"]._leds
    node.read_gate = asyncio.Event()
    node.calls.clear()
    async def read():
        async with controller._lock:
            await controller._read(controller._channels[0], node)
    task = asyncio.create_task(read())
    for _ in range(100):
        if any(method == "get" and args == (14,) for _cc, method, args in node.calls) or task.done():
            break
        await asyncio.sleep(0)
    assert any(method == "get" and args == (14,) for _cc, method, args in node.calls)
    current["node"] = ParameterNode(profile_for_device(hass, row.id))
    controller._refresh()
    node.read_gate.set()
    with pytest.raises(ServiceValidationError):
        await task
    assert controller._confirmed == {}
    assert "color" not in channel(hass, f) and "brightness" not in channel(hass, f)


@pytest.mark.parametrize("previous_complete", [False, True])
@pytest.mark.parametrize("failure", ["timeout", None, 255])
async def test_interrupted_parameter_read_recovers_on_existing_poll_without_writes(hass, monkeypatch, previous_complete, failure):
    polls = []
    monkeypatch.setattr(indicator, "async_track_time_interval",
        lambda _hass, callback, _interval: polls.append(callback) or (lambda: None))
    fail_color = [not previous_complete]

    def interrupt(node):
        node.physical[2] = 3
        node.physical[14] = 2
        invoke = node.invoke

        async def transient_failure(cc, method, *args, **kwargs):
            if method == "get" and args == (14,) and fail_color[0]:
                fail_color[0] = False
                node.calls.append((cc.value, method, args))
                if failure == "timeout":
                    raise TimeoutError("simulated radio timeout")
                return failure
            return await invoke(cc, method, *args, **kwargs)

        node.endpoints[0].async_invoke_cc_api = transient_failure

    f, _row, node, _current = await office(hass, monkeypatch, change=interrupt)
    controller = f["aggregate"]._leds
    try:
        if previous_complete:
            assert channel(hass, f)["color"] == "00FF00"
            fail_color[0] = True
            controller._node_changed({"value": node.values["7-112-0-14"]})
            await controller._task
            await hass.async_block_till_done()
        assert controller._confirmed["status"] == {}
        assert not {"color", "behavior", "brightness"} & channel(hass, f).keys()
        assert channel(hass, f)["available"]
        assert len(polls) == 1
        node.calls.clear()
        polls[0](None)  # Advance the existing interval callback without a real sleep.
        await controller._task
        await hass.async_block_till_done()
        assert [(method, args) for _cc, method, args in node.calls] == [
            ("get", (2,)), ("get", (14,)), ("get", (15,))]
        assert channel(hass, f)["color"] == "00FF00"
        assert channel(hass, f)["behavior"] == "always_on"
        assert channel(hass, f)["brightness"] == 60
        assert len(polls) == 1
    finally:
        await f["aggregate"].async_remove()


@pytest.mark.parametrize("model,parameter", [
    ("ZEN26", 2), ("ZEN27", 2), ("ZEN78", 15),
    ("46201 / ZW4008", 3), ("46562 / ZW4008", 3)])
async def test_on_off_only_models_reuse_metadata_template_without_fake_colors(hass, monkeypatch, model, parameter):
    profile = next(item for item in PROFILES if item.model == model)
    f, row, node, _ = await office(hass, monkeypatch, profile=profile)
    assert profile_for_device(hass, row.id) is profile
    assert channel(hass, f)["colors"] == ["000000", "FFFFFF"]
    assert channel(hass, f)["brightness_supported"] is False
    for data in ({"color": "FF0000"}, {"brightness": 60}, {"color": "FFFFFF", "brightness": 60}):
        node.calls.clear()
        with pytest.raises(ServiceValidationError):
            await command(hass, f, **data)
        assert node.calls == []  # An invalid combined action cannot first power it on.
    await command(hass, f, color="FFFFFF", behavior="always_on")
    assert node.physical == {parameter: 3}
    assert channel(hass, f)["color"] == "FFFFFF"
    await command(hass, f, color="000000", behavior="always_off")
    assert node.physical == {parameter: 2}
    assert channel(hass, f)["color"] == "000000"


@pytest.mark.parametrize("model,channel_ids", [
    ("ZEN30", ["dimmer", "relay"]),
    ("ZEN32", ["relay", "button_1", "button_2", "button_3", "button_4"])])
async def test_multiple_indicators_target_only_the_selected_physical_channel(hass, monkeypatch, model, channel_ids):
    profile = next(item for item in PROFILES if item.model == model)
    f, _row, node, _ = await office(hass, monkeypatch, profile=profile)
    attrs = state_fixture(hass, f["aggregate"])["attributes"]
    assert [item["id"] for item in attrs["bridge_leds"]] == channel_ids
    assert len({item["button_id"] for item in attrs["bridge_leds"]}) == len(channel_ids)
    assert all(method == "get" for _cc, method, _args in node.calls)
    for declaration in profile.channels:
        before = dict(node.physical)
        node.calls.clear()
        await command(hass, f, led_id=declaration.id, color="FF0000", brightness=30, behavior="always_on")
        writes = [args[0] for _cc, method, args in node.calls if method == "set"]
        allowed = {declaration.mode.parameter, declaration.colors[0].schema.parameter,
                   declaration.brightness[0].schema.parameter}
        assert {item["parameter"] for item in writes} == allowed
        assert all(node.physical[param] == value for param, value in before.items() if param not in allowed)
        after = state_fixture(hass, f["aggregate"])["attributes"]["bridge_leds"]
        selected = next(item for item in after if item["id"] == declaration.id)
        assert selected["color"] == "FF0000" and selected["brightness"] == 30


@pytest.mark.parametrize("extended,ten_levels", [(False, True), (True, False), (True, True)])
async def test_zen32_uses_actual_firmware_palette_and_brightness_options(hass, monkeypatch, extended, ten_levels):
    profile = next(item for item in PROFILES if item.model == "ZEN32")
    def variant(node):
        for declaration in profile.channels:
            node.add_parameter(declaration.colors[int(extended)].schema, 1)
            node.add_parameter(declaration.brightness[int(ten_levels)].schema, 1)
    f, _row, node, _ = await office(hass, monkeypatch, profile=profile, change=variant)
    channels = state_fixture(hass, f["aggregate"])["attributes"]["bridge_leds"]
    assert len(channels) == 5
    assert all(len(item["colors"]) == (8 if extended else 5) for item in channels)
    node.calls.clear()
    await command(hass, f, led_id="button_4", color="FF00FF" if extended else "FF0000", brightness=75)
    assert node.physical[10] == (4 if extended else 3)
    assert node.physical[15] == (7 if ten_levels else 1)  # 70% wins the 70/80 tie.
    actual = next(item for item in state_fixture(hass, f["aggregate"])["attributes"]["bridge_leds"] if item["id"] == "button_4")
    assert actual["brightness"] == (70 if ten_levels else 60)


@pytest.mark.parametrize("change", [
    lambda node: node.values["7-112-0-14"].metadata.data.update(isFromConfig=False),
    lambda node: node.values["7-112-0-14"].metadata.data.update(purpose="dimming.max_brightness"),
    lambda node: node.values["7-112-0-14"].metadata.data.update(allowed=[{"from": 0, "to": 2}]),
])
async def test_untrusted_or_contradictory_native_metadata_never_grants_writes(hass, monkeypatch, change):
    f, _row, node, _ = await office(hass, monkeypatch, change=change)
    assert f["aggregate"]._leds.snapshot() == []
    with pytest.raises(ServiceValidationError):
        await command(hass, f, color="FFFFFF")
    assert node.calls == []


async def test_zen32_second_hardware_fingerprint_and_all_channel_validation(hass, monkeypatch):
    profile = next(item for item in PROFILES if item.model == "ZEN32")
    def variant(node):
        node.product_type, node.product_id = profile.fingerprints[1]
    f, _row, node, _ = await office(hass, monkeypatch, profile=profile, change=variant)
    assert len(f["aggregate"]._leds.snapshot()) == 5
    node.calls.clear()
    node.values["7-112-0-10"].metadata.data["label"] = "Unreviewed parameter"
    with pytest.raises(ServiceValidationError):
        await command(hass, f, led_id="relay", color="FF0000")
    assert node.calls == []  # A changed topology cannot retain earlier authority.


async def test_profiles_reject_reused_channel_id_or_configuration_parameter(hass, monkeypatch):
    from dataclasses import replace
    profile = next(item for item in PROFILES if item.model == "ZEN30")
    f, row, _node, _ = await office(hass, monkeypatch, profile=profile)
    duplicate = replace(profile, channels=(profile.channels[0], profile.channels[0]))
    assert discover_parameters(hass, row.id, duplicate) == ()
    shared = replace(profile.channels[1], mode=profile.channels[0].mode)
    assert discover_parameters(hass, row.id, replace(profile, channels=(profile.channels[0], shared))) == ()


async def test_zen32_indicators_follow_actual_scene_button_assignments(hass, monkeypatch):
    from homeassistant.util import dt as dt_util
    from zwave_js_server.model.value import Value

    profile = next(item for item in PROFILES if item.model == "ZEN32")
    assignments = {1: "bottom", 2: "physical_two", 3: "toggle", 4: "physical_four", 5: "top"}
    sources = {}
    def setup_sources(config, row, node):
        config.pop("source_entity_id")
        actions = config.pop("actions")
        actions["KeyPressed2x"] = {"kind": "press", "press_count": 2}
        config["buttons"] = []
        for scene, button_id in assignments.items():
            value = Value(node, {"commandClass": 91, "commandClassName": "Central Scene", "endpoint": 0,
                "property": "scene", "propertyKey": f"{scene:03d}", "ccVersion": 3,
                "metadata": {"type": "number", "readable": False, "writeable": False}, "value": None})
            node.values[value.value_id] = value
            entry = er.async_get(hass).async_get_or_create("event", "zwave_js", f"4070226031.{value.value_id}",
                device_id=row.id, original_name=f"Button {scene}")
            sources[scene] = entry
            hass.states.async_set(entry.entity_id, "unknown", {"event_types": list(actions)})
            config["buttons"].append({"id": button_id, "label": f"Button {scene}",
                "source_entity_id": entry.entity_id, "source_registry_id": entry.id, "actions": deepcopy(actions)})
    f, row, node, _ = await office(hass, monkeypatch, profile=profile, configure_source=setup_sources)
    actual = {item["id"]: item["button_id"] for item in f["aggregate"]._leds.snapshot()}
    assert actual == {"relay": "top", "button_1": "bottom", "button_2": "physical_two",
                      "button_3": "toggle", "button_4": "physical_four"}
    hass.states.async_set(sources[2].entity_id, dt_util.utcnow().isoformat(),
        {"event_types": ["KeyPressed", "KeyPressed2x", "KeyHeldDown", "KeyReleased"], "event_type": "KeyPressed2x"})
    await hass.async_block_till_done()
    attrs = state_fixture(hass, f["aggregate"])["attributes"]
    assert attrs["gesture_sequence"] == 1 and attrs["button_id"] == "physical_two" and attrs["press_count"] == 2
    assert len(attrs["bridge_buttons"]) == len(attrs["bridge_leds"]) == 5


@pytest.mark.parametrize("manufacturer,model", [("GE", "46201"), ("Enbrighten", "46201 / ZW4008"),
                                               ("GE/Enbrighten", "46562"), ("Jasco", "46562 / ZW4008")])
async def test_explicit_registry_brand_aliases_still_require_exact_hardware(hass, monkeypatch, manufacturer, model):
    profile = next(item for item in PROFILES if model.split(" /", 1)[0] in {item.model, *item.model_aliases})
    f, row, node, _ = await office(hass, monkeypatch, profile=profile)
    dr.async_get(hass).async_update_device(row.id, manufacturer=manufacturer, model=model)
    assert profile_for_device(hass, row.id) is profile
    node.manufacturer_id = 0x9999
    node.calls.clear()
    with pytest.raises(ServiceValidationError):
        await command(hass, f, color="FFFFFF")
    assert node.calls == []


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
async def test_color_only_preserves_every_local_led_behavior(hass, monkeypatch, mode):
    f, _row, node, _ = await office(hass, monkeypatch)
    node.physical[2] = mode
    node.calls.clear()
    await command(hass, f, color="FF0000")
    assert [(args[0]["parameter"], args[0]["value"]) for _cc, method, args in node.calls if method == "set"] == [(14, 3)]
    assert node.physical[2] == mode
    assert channel(hass, f)["behavior"] == {0: "follow_load_inverted", 1: "follow_load", 2: "always_off", 3: "always_on"}[mode]
    assert state_fixture(hass, f["aggregate"])["attributes"]["bridge_led_command_version"] == 2


@pytest.mark.parametrize("model,parameter,encodings", [
    ("ZEN76", 2, {"follow_load": 1, "follow_load_inverted": 0, "always_on": 3, "always_off": 2}),
    ("ZEN78", 15, {"follow_load": 0, "follow_load_inverted": 1, "always_on": 3, "always_off": 2}),
])
@pytest.mark.parametrize("behavior", ["follow_load", "follow_load_inverted", "always_on", "always_off"])
async def test_behavior_only_changes_exact_mode_parameter_with_correct_device_encoding(hass, monkeypatch, model, parameter, encodings, behavior):
    profile = next(item for item in PROFILES if item.model == model)
    f, _row, node, _ = await office(hass, monkeypatch, profile=profile)
    before = dict(node.physical)
    node.calls.clear()
    await command(hass, f, behavior=behavior)
    assert [args[0] for _cc, method, args in node.calls if method == "set"] == [{"parameter": parameter, "value": encodings[behavior]}]
    assert all(node.physical[param] == value for param, value in before.items() if param != parameter)
    assert channel(hass, f)["behavior"] == behavior


@pytest.mark.parametrize("data", [{"color": "000000"}, {"color": "000000", "behavior": "always_on"},
    {"behavior": "invalid"}])
async def test_implicit_or_invalid_behavior_is_rejected_before_any_parameter_write(hass, monkeypatch, data):
    f, _row, node, _ = await office(hass, monkeypatch)
    node.calls.clear()
    with pytest.raises((ServiceValidationError, ValueError, vol.Invalid)):
        await command(hass, f, **data)
    assert node.calls == []
