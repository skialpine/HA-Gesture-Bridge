"""Friendly device selection/review using real HA registries and flow managers."""

import json
from pathlib import Path

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN


async def install(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    assert not result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    return result["result"]


def device_fixture(hass, entry, name="Office Switch", events=None):
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_paddles", name)}, name=name)
    entities = []
    for label, event_types in events or [("Upper paddle", ["KeyPressed", "KeyPressed2x", "KeyHeldDown", "KeyReleased"]),
                                        ("Lower paddle", ["KeyPressed", "KeyPressed3x"])]:
        entity = er.async_get(hass).async_get_or_create("event", "fixture_paddles", name+label,
            device_id=device.id, original_name=label, suggested_object_id=name.lower().replace(' ', '_')+'_'+label.lower().replace(' ', '_'))
        hass.states.async_set(entity.entity_id, "unknown", {"event_types": event_types})
        entities.append(entity)
    return device, entities


def default_input(result):
    return result["data_schema"]({})


async def start_control(hass, entry, device, *, leds=False):
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    assert result["step_id"] == "user" and result["last_step"] is False
    assert {key.schema for key in result["data_schema"].schema} == {"device_id", "configure_leds"}
    return await hass.config_entries.subentries.async_configure(result["flow_id"], {"device_id": device.id, "configure_leds": leds})


async def test_legacy_controls_are_imported_only_when_requested(hass):
    config = {"unique_id": "existing-native", "name": "Existing Native", "source_entity_id": "event.paddle",
              "button_id": "top", "button_label": "Top",
              "actions": {"KeyPressed": {"kind": "press", "press_count": 1}}}
    legacy = {"existing-native": {"config": config}}
    hass.data[DOMAIN] = {"yaml_controls": legacy}
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert default_input(form) == {"import_yaml": False}
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert not result["result"].subentries and not result["result"].data["imported_yaml"]
    assert hass.data[DOMAIN]["yaml_controls"]["existing-native"] == {"config": config}


async def test_initial_yaml_import_requires_mqtt_and_retries_without_partial_import(hass, monkeypatch):
    from homeassistant.components import mqtt

    config = {"unique_id": "existing-mqtt", "name": "Existing MQTT", "sources": [{
        "id": "remote", "type": "mqtt", "topic": "fixture/action",
        "buttons": [{"id": "top", "label": "Top"}], "mappings": [{"button_id": "top",
            "match": {"action": "single"}, "gesture": {"kind": "press", "press_count": 1}}]}]}
    hass.data[DOMAIN] = {"yaml_controls": {"existing-mqtt": {"config": config}}}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": True})
    assert result["type"] == "form" and result["errors"] == {"base": "mqtt_not_configured"}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert hass.data[DOMAIN]["yaml_controls"]["existing-mqtt"]["config"] == config
    async def subscribe(*_args, **_kwargs):
        return lambda: None
    hass.config.components.add("mqtt")
    monkeypatch.setattr(mqtt, "is_connected", lambda _hass: True)
    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_subscribe_connection_status", lambda *_args: lambda: None)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": True})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert next(iter(result["result"].subentries.values())).unique_id == "existing-mqtt"


async def test_choose_native_device_review_and_save_one_aggregate(hass):
    entry = await install(hass)
    device, entities = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    assert review["step_id"] == "review" and review["last_step"] is True
    values = default_input(review)
    assert values["name"] == "Office Switch Gestures"
    assert values["top_button"] != values["bottom_button"]
    assert values["toggle_button"] == "__none__"
    summary = review["description_placeholders"]["inputs"]
    assert "Upper paddle" in summary and "Lower paddle" in summary
    assert "Double tap" in summary and "Triple tap" in summary
    for key, value in review["data_schema"].schema.items():
        if key.schema.endswith("_button"):
            assert all('label' in option and 'value' in option for option in value.config["options"])
            assert len(value.config["options"]) == 3  # Two actual inputs plus None; no JSON modal.
    saved = await hass.config_entries.subentries.async_configure(review["flow_id"], values | {"load_entity_id": "light.office"})
    assert saved["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    assert control.data["source_device_id"] == device.id
    assert control.data["load_entity_id"] == "light.office"
    assert {button["id"] for button in control.data["buttons"]} == {"top", "bottom"}
    assert len(control.data["buttons"]) == 2
    entity_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, control.unique_id)
    aggregates = [s for s in hass.states.async_all("event") if s.attributes.get("bridge_control_id") == control.unique_id]
    assert len(aggregates) == 1 and aggregates[0].entity_id == entity_id
    source = entities[0]
    hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(), {"event_types": ["KeyPressed", "KeyPressed2x"], "event_type": "KeyPressed2x"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["gesture_sequence"] == 1  # One input subscription, no duplicated gesture.
    assert hass.states.get(entity_id).attributes["press_count"] == 2


async def test_five_button_device_exports_each_input_without_collapsing_roles(hass):
    entry = await install(hass)
    labels = ["Button 1", "Button 2", "Button 3", "Button 4", "Relay button"]
    device, entities = device_fixture(hass, entry, name="Five Button Controller",
        events=[(label, ["KeyPressed", "KeyPressed2x", "KeyHeldDown", "KeyReleased"]) for label in labels])
    review = await start_control(hass, entry, device)
    assert review["description_placeholders"]["inputs"].count("Double tap") == 5
    saved = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert saved["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    aggregate_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, control.unique_id)
    configured = {button["source_entity_id"]: button["id"] for button in control.data["buttons"]}
    initial = hass.states.get(aggregate_id)
    assert len(initial.attributes["bridge_buttons"]) == len(set(configured.values())) == 5
    assert all(source["ready"] for source in initial.attributes["bridge_sources"])
    assert "gesture_sequence" not in initial.attributes
    for sequence, source in enumerate(entities, 1):
        hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(), {
            "event_types": ["KeyPressed", "KeyPressed2x", "KeyHeldDown", "KeyReleased"],
            "event_type": "KeyPressed2x"})
        await hass.async_block_till_done()
        attrs = hass.states.get(aggregate_id).attributes
        assert attrs["button_id"] == configured[source.entity_id]
        assert attrs["gesture_id"] == "press_2" and attrs["gesture_sequence"] == sequence
    last = hass.states.get(aggregate_id)
    hass.states.async_set(entities[0].entity_id, "unavailable")
    await hass.async_block_till_done()
    after = hass.states.get(aggregate_id)
    assert after.attributes["gesture_event_id"] == last.attributes["gesture_event_id"]
    readiness = {source["button_id"]: source["ready"] for source in after.attributes["bridge_sources"]}
    assert readiness[configured[entities[0].entity_id]] is False
    assert sum(readiness.values()) == 4


@pytest.mark.parametrize("detected", ["Status indicator: on/off and brightness", "Zooz status LED: on/off, colors and brightness"])
async def test_automatic_led_summary_appears_without_custom_mapping_step(hass, monkeypatch, detected):
    from custom_components.ha_gesture_bridge import config_flow

    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    discovered = []

    def summary(_hass, device_id):
        discovered.append(device_id)
        return detected

    monkeypatch.setattr(config_flow, "led_summary", summary)
    review = await start_control(hass, entry, device)
    assert discovered == [device.id]
    assert review["description_placeholders"]["led_summary"] == detected
    assert review["step_id"] == "review" and review["last_step"] is True
    assert "configuration" not in {key.schema for key in review["data_schema"].schema}
    saved = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert saved["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    assert control.data["source_device_id"] == device.id
    assert not control.data.get("leds")  # Capabilities are rediscovered, not copied into hand-written maps.


async def test_unknown_device_and_no_input_device_give_actionable_errors(hass):
    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("fixture", "load")}, name="Load only")
    result = await start_control(hass, entry, device)
    assert result["step_id"] == "user" and result["errors"] == {"device_id": "no_supported_inputs"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"device_id": "0"*32})
    assert result["errors"] == {"device_id": "device_unavailable"}
    assert not entry.subentries


async def test_review_rejects_duplicate_roles_empty_name_and_changed_capabilities(hass):
    entry = await install(hass)
    device, entities = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    values = default_input(review)
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values | {"bottom_button": values["top_button"]})
    assert result["errors"] == {"base": "invalid_control"}
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values | {"name": " "})
    assert result["errors"] == {"base": "invalid_control"}
    hass.states.async_set(entities[0].entity_id, "unknown", {"event_types": ["KeyPressed"]})
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values)
    assert result["errors"] == {"base": "device_changed"}
    assert not entry.subentries
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(result))
    assert result["type"] == "create_entry"


async def test_reconfigure_preselects_device_and_preserves_roles_without_json(hass):
    entry = await install(hass)
    device, entities = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    values = default_input(review)
    saved = await hass.config_entries.subentries.async_configure(review["flow_id"], values)
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    identity, roles = control.unique_id, dict(control.data["source_button_roles"])
    aggregate_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, identity)
    original_source = hass.states.get(entities[0].entity_id)
    er.async_get(hass).async_update_entity(entities[0].entity_id, new_entity_id="event.renamed_upper")
    # The source integration owns the HA state move; our fixture updates that boundary.
    hass.states.async_set("event.renamed_upper", original_source.state, dict(original_source.attributes))
    hass.states.async_remove(entities[0].entity_id)
    await hass.async_block_till_done()
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    assert default_input(flow)["device_id"] == device.id
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
    assert review["step_id"] == "review" and "configuration" not in default_input(review)
    values = default_input(review)
    assert values["name"] == "Office Switch Gestures"
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values | {"name": "Renamed Paddle"})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert control.unique_id == identity and control.data["source_button_roles"] == roles
    assert control.data["name"] == "Renamed Paddle"
    assert er.async_get(hass).async_get_entity_id("event", DOMAIN, identity) == aggregate_id
    assert any(button["source_entity_id"] == "event.renamed_upper" for button in control.data["buttons"])


async def test_custom_buttons_remain_original_and_single_button_defaults_toggle(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry, "Scene remote", [("Scene one", ["single"]), ("Scene two", ["double"])])
    review = await start_control(hass, entry, device)
    assert all(default_input(review)[role+"_button"] == "__none__" for role in ("top", "bottom", "toggle"))
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert result["type"] == "create_entry"
    control = next(iter(entry.subentries.values()))
    assert {button["label"] for button in control.data["buttons"]} == {"Scene one", "Scene two"}
    assert set(control.data["source_button_roles"]) == set(control.data["source_button_roles"].values())
    device, _ = device_fixture(hass, entry, "Single remote", [("Main", ["single"])])
    review = await start_control(hass, entry, device)
    assert default_input(review)["toggle_button"] != "__none__"


async def test_led_configuration_is_optional_after_review_and_preserved_on_edit(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    backend = er.async_get(hass).async_get_or_create("select", "fixture_led", "color", suggested_object_id="fixture_led_color")
    review = await start_control(hass, entry, device, leds=True)
    assert review["step_id"] == "review_leds" and review["last_step"] is False
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert result["step_id"] == "leds" and result["last_step"] is True and not entry.subentries
    invalid = '[{"id":"status","label":"Status","button_id":"top","color":{"entity_id":"light.relay","values":{"FFFFFF":"on"}}}]'
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": invalid})
    assert result["errors"] == {"base": "invalid_led_mapping"} and "select" in result["description_placeholders"]["error"]
    assert default_input(result)["configuration"] == invalid
    leds = [{"id": "status", "label": "Status", "button_id": "top", "color": {"entity_id": backend.entity_id, "values": {"FFFFFF": "White"}}}]
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": json.dumps(leds)})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    assert control.data["leds"][0]["color"]["registry_id"] == backend.id
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
    assert review["description_placeholders"]["led_notice"] == ""
    assert "Custom LED mappings are active" in review["description_placeholders"]["led_summary"]
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert result["type"] == "abort" and control.data["leds"][0]["id"] == "status"
    await hass.async_block_till_done()
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={
        "source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "device_id": device.id, "configure_leds": True})
    form = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], {"configuration": "[]"})
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert control.data["leds"] == [] and control.data["source_device_id"] == device.id


def test_translations_use_supported_save_override_and_no_protocol_chooser():
    component = Path(__file__).resolve().parents[2] / "custom_components/ha_gesture_bridge"
    for path in (component / "strings.json", component / "translations/en.json"):
        strings = json.loads(path.read_text())
        steps = strings["config_subentries"]["control"]["step"]
        assert steps["review"]["submit"] == steps["leds"]["submit"] == "Save"
        assert steps["review_leds"]["submit"] == "Next"
        assert set(steps) == {"user", "review", "review_leds", "leds"}
        assert "source_type" not in json.dumps(strings)
        assert steps["user"]["data"]["configure_leds"] == "Customize LED mappings (advanced)"
        assert "{led_summary}" in steps["review"]["description"]
        assert "{led_summary}" in steps["review_leds"]["description"]
        assert "Quick Setup" in strings["config"]["step"]["user"]["description"]
        for step in ("review", "review_leds"):
            assert "Save with the field empty to leave it unlinked." in steps[step]["description"]


async def test_removed_device_cannot_be_saved_after_review_or_optional_led_step(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    values = default_input(review)
    dr.async_get(hass).async_remove_device(device.id)
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values)
    assert result["errors"] == {"base": "device_unavailable"} and not entry.subentries
    device, _ = device_fixture(hass, entry, "Second Switch")
    review = await start_control(hass, entry, device, leds=True)
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert result["step_id"] == "leds"
    dr.async_get(hass).async_remove_device(device.id)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": "[]"})
    assert result["step_id"] == "leds" and result["errors"] == {"base": "device_unavailable"}
    assert not entry.subentries


async def test_long_device_name_still_defaults_to_valid_control_name(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry, "X"*128)
    review = await start_control(hass, entry, device)
    values = default_input(review)
    assert len(values["name"]) == 128 and values["name"].endswith(" Gestures")
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()


async def test_same_device_review_uses_ha_trigger_fallback_and_fires_exactly_once(hass, monkeypatch):
    from test_device_discovery import provider, trigger

    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_remote", "scene")}, name="Scene Remote")
    calls, callbacks, _, _ = provider(hass, monkeypatch, [
        trigger(device.id, "remote_button_short_press"), trigger(device.id, "remote_button_double_press")])
    review = await start_control(hass, entry, device)
    assert review["step_id"] == "review" and "Double tap" in review["description_placeholders"]["inputs"]
    values = default_input(review)
    assert values["toggle_button"] != "__none__"
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], values)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    assert "buttons" not in control.data and control.data["sources"][0]["type"] == "ha_device_trigger"
    assert len(calls) == len(callbacks) == 2  # Trigger provider attached once each, no native duplicate path.
    entity_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, control.unique_id)
    double = next(index for index, (config, _info) in enumerate(calls) if config["type"] == "remote_button_double_press")
    await callbacks[double]({"trigger": {}})
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["gesture_sequence"] == 1 and state.attributes["press_count"] == 2


async def test_same_gesture_with_changed_raw_action_cannot_save_stale_led_pending_mapping(hass):
    entry = await install(hass)
    device, entities = device_fixture(hass, entry, events=[("Main", ["single"])])
    review = await start_control(hass, entry, device, leds=True)
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert result["step_id"] == "leds"
    # Both names normalize to press_1, but the source action to subscribe/map changed.
    hass.states.async_set(entities[0].entity_id, "unknown", {"event_types": ["KeyPressed"]})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": "[]"})
    assert result["errors"] == {"base": "device_changed"} and not entry.subentries


async def test_device_cannot_create_duplicate_controls_but_its_existing_control_can_be_edited(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    second = await start_control(hass, entry, device)
    saved = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    assert saved["type"] == "create_entry"
    duplicate = await hass.config_entries.subentries.async_configure(second["flow_id"], default_input(second))
    assert duplicate["type"] == "abort" and duplicate["reason"] == "device_already_configured"
    duplicate = await start_control(hass, entry, device)
    assert duplicate["errors"] == {"device_id": "device_already_configured"}
    assert len(entry.subentries) == 1
    control = next(iter(entry.subentries.values()))
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
    assert review["step_id"] == "review"
    await hass.async_block_till_done()


async def test_unsupported_device_configuration_shows_actionable_detail_without_provider_secrets(hass, monkeypatch):
    from test_device_discovery import mqtt_fixture

    entry = await install(hass)
    device, targets, _, _ = await mqtt_fixture(hass, monkeypatch)
    targets["single"].value_template = "{{ value_json.private_action }}"
    result = await start_control(hass, entry, device)
    assert result["step_id"] == "user" and result["errors"] == {"device_id": "unsupported_device_configuration"}
    assert "value templates" in result["description_placeholders"]["hint"]
    assert "private_action" not in json.dumps(result["description_placeholders"])
    assert not entry.subentries


async def test_provider_unavailability_is_a_friendly_retryable_device_error(hass, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_remote", "unavailable")}, name="Offline Remote")
    async def unavailable(*_args):
        raise HomeAssistantError("private connection details")
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", unavailable)
    result = await start_control(hass, entry, device)
    assert result["step_id"] == "user" and result["errors"] == {"device_id": "device_unavailable"}
    assert "private" not in json.dumps(result["description_placeholders"])
    assert not entry.subentries


async def test_switching_selected_device_recommends_its_name_without_replacing_saved_custom_name(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    assert review["description_placeholders"]["suggested_name"] == "Office Switch Gestures"
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review) | {"name": "My Custom Paddle"})
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    assert control.title == control.data["name"] == "My Custom Paddle"
    replacement, _ = device_fixture(hass, entry, "Replacement Switch")
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": replacement.id})
    assert review["description_placeholders"]["suggested_name"] == "Replacement Switch Gestures"
    assert default_input(review)["name"] == "My Custom Paddle"


@pytest.mark.parametrize("configure_leds", [False, True])
async def test_changing_device_removes_previous_led_authority_with_visible_notice(hass, configure_leds):
    entry = await install(hass)
    original_device, _ = device_fixture(hass, entry, "Original Switch")
    replacement, _ = device_fixture(hass, entry, "Replacement Switch")
    backend = er.async_get(hass).async_get_or_create("select", "fixture_led", "original-color",
        suggested_object_id="original_switch_led_color", device_id=original_device.id)
    leds = [{"id": "status", "label": "Original LED", "button_id": "top",
             "color": {"entity_id": backend.entity_id, "values": {"FFFFFF": "White"}}}]
    review = await start_control(hass, entry, original_device, leds=True)
    form = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], {"configuration": json.dumps(leds)})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    assert control.data["leds"][0]["color"]["entity_id"] == backend.entity_id
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "device_id": replacement.id, "configure_leds": configure_leds})
    assert "Previous device LED mappings will be removed" in review["description_placeholders"]["led_notice"]
    result = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review))
    if configure_leds:
        assert result["step_id"] == "leds" and json.loads(default_input(result)["configuration"]) == []
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": "[]"})
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert control.data["source_device_id"] == replacement.id
    assert not control.data.get("leds") and backend.entity_id not in json.dumps(dict(control.data))
    aggregate_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, control.unique_id)
    assert not hass.states.get(aggregate_id).attributes.get("bridge_leds")


def linked_target_suggestion(review):
    field = next(field for field in review["data_schema"].schema if field.schema == "load_entity_id")
    return (field.description or {}).get("suggested_value")


@pytest.mark.parametrize("configure_leds, target", [(False, None), (True, "switch.new_load")])
async def test_changed_source_requires_reselecting_the_linked_target(hass, configure_leds, target):
    entry = await install(hass)
    original, _ = device_fixture(hass, entry, "Original Switch")
    replacement, _ = device_fixture(hass, entry, "Replacement Switch")
    review = await start_control(hass, entry, original)
    old_target = "light.old_load"
    result = await hass.config_entries.subentries.async_configure(review["flow_id"],
        default_input(review) | {"load_entity_id": old_target})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "device_id": replacement.id, "configure_leds": configure_leds})
    assert next(iter(review["data_schema"].schema)).schema == "load_entity_id"
    assert linked_target_suggestion(review) is None
    values = default_input(review)
    if target:
        values["load_entity_id"] = target
    retry = await hass.config_entries.subentries.async_configure(review["flow_id"], values | {"name": ""})
    assert retry["errors"] == {"base": "invalid_control"}
    assert linked_target_suggestion(retry) == target
    assert control.data["source_device_id"] == original.id and control.data["load_entity_id"] == old_target
    result = await hass.config_entries.subentries.async_configure(retry["flow_id"], values)
    if configure_leds:
        assert result["step_id"] == "leds"
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": "[]"})
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert control.data["source_device_id"] == replacement.id and control.data.get("load_entity_id") == target


async def test_same_source_suggests_existing_target_until_user_clears_it(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    review = await start_control(hass, entry, device)
    old_target = "light.office"
    result = await hass.config_entries.subentries.async_configure(review["flow_id"],
        default_input(review) | {"load_entity_id": old_target})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    review = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
    assert linked_target_suggestion(review) == old_target
    retry = await hass.config_entries.subentries.async_configure(review["flow_id"], default_input(review) | {"name": ""})
    assert retry["errors"] == {"base": "invalid_control"} and linked_target_suggestion(retry) is None
    assert control.data["load_entity_id"] == old_target
