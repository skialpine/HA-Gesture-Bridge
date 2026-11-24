"""LED configuration, actual HA services and confirmed feedback lifecycle."""

import asyncio
from copy import deepcopy
import json

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
import voluptuous as vol

from custom_components.ha_gesture_bridge import enrich_native_sources
from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig
from custom_components.ha_gesture_bridge.led_config import parse_leds
from tools.generate_led_fixture import setup_fixture, state_fixture


async def command(hass, fixture, **data):
    await hass.services.async_call(DOMAIN, "set_led", {
        "entity_id": fixture["aggregate"].entity_id, "led_id": "status", **data}, blocking=True)
    await hass.async_block_till_done()


def channel(hass, fixture):
    return state_fixture(hass, fixture["aggregate"])["attributes"]["bridge_leds"][0]


async def wait_for_calls(fixture):
    async with asyncio.timeout(1):
        while not fixture["calls"]:
            await asyncio.sleep(0)


async def test_actual_services_publish_confirmed_snapshot_without_gestures(hass):
    f = await setup_fixture(hass)
    assert f["calls"] == []  # Setup/reconnect never controls an LED or load.
    attrs = state_fixture(hass, f["aggregate"])["attributes"]
    assert attrs["bridge_led_version"] == 1
    assert channel(hass, f) == {"id": "status", "label": "Status LED", "button_id": "toggle",
        "available": True, "colors": ["000000", "0000FF", "00FF00", "FF0000", "FFFFFF"],
        "brightness_supported": True, "brightness_levels": [30, 60, 100],
        "color": "FFFFFF", "configured_color": "FFFFFF", "brightness": 100,
        "color_supported": True, "behaviors": ["always_on", "always_off"], "behavior": "always_on"}
    assert not any(value.entity_id in json.dumps(attrs["bridge_leds"]) for value in (f["color"], f["power"], f["brightness"]))
    f["paddle"]._trigger_event("KeyPressed")
    f["paddle"].async_write_ha_state()
    await hass.async_block_till_done()
    before = state_fixture(hass, f["aggregate"])["attributes"]
    assert before["gesture_sequence"] == 1
    await command(hass, f, color="000000", behavior="always_off")
    assert f["calls"] == [(f["power"].entity_id, "Always off")]
    assert f["color"].current_option == "White"  # Off preserves configured color.
    assert channel(hass, f)["color"] == "000000"
    f["calls"].clear()
    await command(hass, f, color="0000ff", brightness=80, behavior="always_on")  # Tie resolves to 60%; number value is inverted.
    assert f["calls"] == [(f["color"].entity_id, "Blue"), (f["power"].entity_id, "Always on"), (f["brightness"].entity_id, 1)]
    assert channel(hass, f)["color"] == "0000FF" and channel(hass, f)["brightness"] == 60
    after = state_fixture(hass, f["aggregate"])["attributes"]
    assert after["bridge_update_kind"] == "snapshot"
    for key in ("gesture_sequence", "gesture_id", "gesture_occurred_at", "bridge_session_id"):
        assert after.get(key) == before.get(key)


async def test_no_optimistic_feedback_and_known_unmapped_option(hass):
    f = await setup_fixture(hass, feedback=False)
    before = hass.states.get(f["aggregate"].entity_id)
    await command(hass, f, color="0000FF")
    assert f["calls"] == [(f["color"].entity_id, "Blue")]
    assert hass.states.get(f["aggregate"].entity_id) is before
    assert channel(hass, f)["color"] == "FFFFFF"
    f["color"]._attr_current_option = "Blue"
    f["color"].async_write_ha_state()
    await hass.async_block_till_done()
    assert channel(hass, f)["color"] == "0000FF"
    f["power"]._attr_current_option = "Load dependent"
    f["power"].async_write_ha_state()
    await hass.async_block_till_done()
    assert channel(hass, f)["available"] and "color" not in channel(hass, f)
    await command(hass, f, color="FF0000")
    assert f["calls"][-1:] == [(f["color"].entity_id, "Red")]
    assert f["power"].current_option == "Load dependent"
    assert "behavior" not in channel(hass, f)


@pytest.mark.parametrize("data", [{}, {"color": "111111"}, {"led_id": "unknown", "color": "FFFFFF"}])
async def test_service_rejects_unknown_or_incomplete_request_without_writes(hass, data):
    f = await setup_fixture(hass)
    with pytest.raises(ServiceValidationError):
        await command(hass, f, **data)
    assert f["calls"] == []


@pytest.mark.parametrize("data", [{"color": "#FFFFFF"}, {"brightness": True}, {"brightness": float('nan')},
                                   {"brightness": 101}, {"color": "FFFFFF", "option": "On"}])
async def test_service_schema_rejects_invalid_values(hass, data):
    f = await setup_fixture(hass)
    with pytest.raises((vol.Invalid, ValueError)):
        await command(hass, f, **data)
    assert f["calls"] == []


@pytest.mark.parametrize("endpoint,change", [("color", {"state": "unavailable"}),
    ("color", {"options": ["White", "Blue"]}), ("power", {"state": "unknown"}),
    ("brightness", {"max": 1}), ("brightness", {"step": .7}), ("color", {"restored": True})])
async def test_endpoint_validation_preflights_entire_action(hass, endpoint, change):
    f = await setup_fixture(hass)
    entity = f[endpoint]
    state = hass.states.get(entity.entity_id)
    attributes = dict(state.attributes)
    attributes.update({key: value for key, value in change.items() if key != "state"})
    hass.states.async_set(entity.entity_id, change.get("state", state.state), attributes)
    await hass.async_block_till_done()
    assert not channel(hass, f)["available"]
    with pytest.raises(ServiceValidationError):
        await command(hass, f, color="0000FF", brightness=60)
    assert f["calls"] == []


async def test_unsupported_brightness_never_writes_color_and_power_only_is_explicit(hass):
    def configure(config):
        config["leds"][0].pop("color")
        config["leds"][0].pop("brightness")
    f = await setup_fixture(hass, configure=configure)
    assert channel(hass, f)["colors"] == ["000000", "FFFFFF"]
    assert not channel(hass, f)["brightness_supported"]
    with pytest.raises(ServiceValidationError, match="brightness"):
        await command(hass, f, color="000000", brightness=60, behavior="always_off")
    assert f["calls"] == []
    await command(hass, f, color="000000", behavior="always_off")
    await command(hass, f, color="FFFFFF", behavior="always_on")
    assert f["calls"] == [(f["power"].entity_id, "Always off"), (f["power"].entity_id, "Always on")]


async def test_pinned_registry_rename_delete_and_replacement_never_rebind(hass):
    f = await setup_fixture(hass)
    registry = er.async_get(hass)
    source = registry.async_get(f["color"].entity_id)
    assert source is not None
    enriched = enrich_native_sources(hass, f["config"])
    assert enriched["leds"][0]["color"]["registry_id"] == source.id
    original = source.entity_id
    registry.async_update_entity(original, new_entity_id="select.renamed_fixture_color")
    await hass.async_block_till_done()
    assert f["color"].entity_id == "select.renamed_fixture_color"
    assert channel(hass, f)["available"]
    await command(hass, f, color="0000FF")
    assert f["calls"] == [("select.renamed_fixture_color", "Blue")]
    assert enrich_native_sources(hass, enriched)["leds"][0]["color"]["entity_id"] == "select.renamed_fixture_color"
    registry.async_remove("select.renamed_fixture_color")
    registry.async_get_or_create("select", "replacement", "other-hardware", suggested_object_id="renamed_fixture_color")
    hass.states.async_set("select.renamed_fixture_color", "White", {"options": ["White", "Blue", "Red", "Green"]})
    await hass.async_block_till_done()
    assert not channel(hass, f)["available"]
    with pytest.raises(ServiceValidationError):
        await command(hass, f, color="FF0000")
    assert len(f["calls"]) == 1


async def test_multistep_actions_serialize_and_unload_cancels_remaining_writes(hass):
    f = await setup_fixture(hass)
    gate = f["color"].gate = asyncio.Event()
    tasks = [hass.async_create_task(hass.services.async_call(DOMAIN, "set_led", {
        "entity_id": f["aggregate"].entity_id, "led_id": "status", "color": "0000FF", "brightness": 60}, blocking=True))]
    try:
        await wait_for_calls(f)
        tasks.append(hass.async_create_task(hass.services.async_call(DOMAIN, "set_led", {
            "entity_id": f["aggregate"].entity_id, "led_id": "status", "color": "FF0000"}, blocking=True)))
        await asyncio.sleep(0)
        assert f["calls"] == [(f["color"].entity_id, "Blue")]
        # Unload during the first awaited hardware write. No later endpoint or queued action can run.
        await f["aggregate"].async_remove()
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        gate.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ServiceValidationError) for result in results)
    assert f["calls"] == [(f["color"].entity_id, "Blue")]
    with pytest.raises(ServiceValidationError, match="not currently loaded"):
        await command(hass, f, color="FFFFFF")


async def test_concurrent_actions_remain_whole_instead_of_interleaving(hass):
    f = await setup_fixture(hass)
    gate = f["color"].gate = asyncio.Event()
    async def call(**data):
        await hass.services.async_call(DOMAIN, "set_led", {"entity_id": f["aggregate"].entity_id,
            "led_id": "status", **data}, blocking=True)
    tasks = [asyncio.create_task(call(color="0000FF", brightness=60))]
    try:
        await wait_for_calls(f)
        tasks.append(asyncio.create_task(call(color="FF0000", brightness=30)))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(*tasks)
    finally:
        gate.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert f["calls"] == [(f["color"].entity_id, "Blue"), (f["brightness"].entity_id, 1),
                          (f["color"].entity_id, "Red"), (f["brightness"].entity_id, 2)]


def led_config():
    return {"id": "status", "label": "Status LED", "button_id": "toggle",
            "color": {"entity_id": "select.color", "values": {"FFFFFF": "White", "0000FF": "Blue"}},
            "power": {"entity_id": "select.mode", "on": "Always on", "off": "Always off"},
            "brightness": {"entity_id": "number.brightness", "values": {"100": 0, "60": 1}}}


@pytest.mark.parametrize("change", [
    {"button_id": "Toggle"}, {"id": "invalid id"}, {"label": "x\nname"}, {"label": "x" * 129},
    {"color": {"entity_id": "light.relay", "values": {"FFFFFF": "on"}}},
    {"power": {"entity_id": "switch.relay", "on": "on", "off": "off"}},
    {"color": {"entity_id": "select.color", "values": {"000000": "Off"}}},
    {"color": {"entity_id": "select.color", "values": {"ffffff": "White", "FFFFFF": "Other"}}},
    {"color": {"entity_id": "select.color", "values": {"FFFFFF": "White", "0000FF": "White"}}},
    {"power": {"entity_id": "select.color", "on": "Always on", "off": "Always off"}},
    {"brightness": {"entity_id": "number.brightness", "values": {"010": 1}}},
    {"brightness": {"entity_id": "number.brightness", "values": {"101": 1}}},
    {"brightness": {"entity_id": "number.brightness", "values": {"100": True}}},
    {"brightness": {"entity_id": "number.brightness", "values": {"100": float('inf')}}},
    {"color": {"entity_id": "select.color", "values": {f'{i+1:06X}': str(i) for i in range(65)}}},
    {"unknown": True},
])
def test_invalid_led_config_rejects_relays_and_ambiguous_capabilities(change):
    with pytest.raises(ValueError):
        parse_leds([led_config() | change])


def test_endpoint_and_button_reuse_and_empty_channel_are_rejected():
    config = led_config()
    for second in (deepcopy(config), config | {"id": "other", "button_id": "top"}):
        with pytest.raises(ValueError):
            parse_leds([config, second])
    with pytest.raises(ValueError):
        parse_leds([{"id": "status", "label": "Status", "button_id": "toggle", "brightness": config["brightness"]}])
    second = deepcopy(config)
    for key in ("color", "power", "brightness"):
        config[key]["registry_id"] = f"uuid-{key}"
        second[key]["entity_id"] += "_other"
        second[key]["registry_id"] = config[key]["registry_id"]
    second.update(id="other", button_id="top")
    with pytest.raises(ValueError, match="reuse"):
        parse_leds([config, second])
    assert parse_leds([]) == ()


@pytest.mark.parametrize("role", ["color", "power", "brightness"])
async def test_generic_select_and_number_backends_are_not_role_specific(hass, role):
    from homeassistant.components.number import DATA_COMPONENT as NUMBERS
    from homeassistant.components.select import DATA_COMPONENT as SELECTS
    from homeassistant.setup import async_setup_component
    from tools.generate_led_fixture import FixtureNumber, FixtureSelect

    calls = []
    if role == "brightness":
        assert await async_setup_component(hass, "select", {})
        backend = FixtureSelect("Fixture discrete intensity", ["Full", "Medium", "Low"], "Full", calls)
        await hass.data[SELECTS].async_add_entities([backend])
        endpoint = {"entity_id": backend.entity_id, "values": {"100": "Full", "60": "Medium", "30": "Low"}}
    else:
        assert await async_setup_component(hass, "number", {})
        backend = FixtureNumber(calls)
        backend._attr_unique_id = backend._attr_name = "Fixture numeric " + role
        await hass.data[NUMBERS].async_add_entities([backend])
        endpoint = ({"entity_id": backend.entity_id, "values": {"FFFFFF": 0, "0000FF": 1, "FF0000": 2}}
                    if role == "color" else {"entity_id": backend.entity_id, "on": 0, "off": 1})
    f = await setup_fixture(hass, configure=lambda config: config["leds"][0].update({role: endpoint}))
    await command(hass, f, **({"brightness": 60} if role == "brightness" else {"color": "0000FF"} if role == "color" else {"behavior": "always_off"}))
    assert calls == [(backend.entity_id, "Medium" if role == "brightness" else 1)]
    assert channel(hass, f)["brightness" if role == "brightness" else "color"] == (60 if role == "brightness" else "0000FF" if role == "color" else "000000")


async def test_color_only_has_no_black_and_diagnostics_never_expose_led_endpoints(hass):
    from types import SimpleNamespace
    from custom_components.ha_gesture_bridge.diagnostics import async_get_config_entry_diagnostics

    f = await setup_fixture(hass, configure=lambda config: config["leds"][0].pop("power"))
    assert "000000" not in channel(hass, f)["colors"]
    with pytest.raises(ServiceValidationError, match="palette"):
        await command(hass, f, color="000000")
    enriched = enrich_native_sources(hass, f["config"])
    entry = SimpleNamespace(subentries={"control": SimpleNamespace(subentry_type="control", data=enriched)})
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for endpoint in enriched["leds"][0].values():
        if isinstance(endpoint, dict):
            assert endpoint["entity_id"] not in diagnostics
            assert endpoint["registry_id"] not in diagnostics
    for private in ("Status LED", "fixture_led_color", "White", "Blue"):
        assert private not in diagnostics
    assert not f["calls"]


async def test_no_led_configuration_advertises_no_led_capabilities(hass):
    f = await setup_fixture(hass, configure=lambda config: config.pop("leds"))
    attrs = state_fixture(hass, f["aggregate"])["attributes"]
    assert "bridge_led_version" not in attrs and "bridge_leds" not in attrs
    with pytest.raises(ServiceValidationError, match="not currently loaded"):
        await command(hass, f, color="FFFFFF")
    assert not f["calls"]


async def test_manual_mapping_color_only_keeps_power_off_and_behavior_is_separate(hass):
    f = await setup_fixture(hass)
    await command(hass, f, behavior="always_off")
    f["calls"].clear()
    await command(hass, f, color="FF0000")
    assert f["calls"] == [(f["color"].entity_id, "Red")]
    assert f["power"].current_option == "Always off"
    assert channel(hass, f)["behavior"] == "always_off" and channel(hass, f)["color"] == "000000"
    assert channel(hass, f)["configured_color"] == "FF0000"
    f["calls"].clear()
    await command(hass, f, behavior="always_on")
    assert f["calls"] == [(f["power"].entity_id, "Always on")]
    assert channel(hass, f)["color"] == "FF0000"


@pytest.mark.parametrize("data", [{"color": "000000"}, {"color": "000000", "behavior": "always_on"},
    {"behavior": "follow_load"}, {"behavior": "invalid"}])
async def test_manual_mapping_requires_supported_explicit_behavior(hass, data):
    f = await setup_fixture(hass)
    with pytest.raises((ServiceValidationError, ValueError, vol.Invalid)):
        await command(hass, f, **data)
    assert f["calls"] == []
