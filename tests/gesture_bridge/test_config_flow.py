"""Exercise the real HA installation/subentry flow managers."""

import json

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN


async def install(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": False})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    return result["result"]


async def control_flow(hass, entry, source="native_event"):
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    return await hass.config_entries.subentries.async_configure(result["flow_id"], {"name": "Office Paddle", "source_type": source})


@pytest.mark.asyncio
async def test_native_profile_only_advertises_actual_source_events(hass):
    entry = await install(hass)
    hass.states.async_set("event.office_top", "unknown", {"event_types": ["KeyPressed", "KeyPressed2x", "vendor_unknown"]})
    hass.states.async_set("event.office_bottom", "unknown", {"event_types": ["KeyReleased"]})
    result = await control_flow(hass, entry)
    assert result["step_id"] == "native"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "top_entity_id": "event.office_top", "bottom_entity_id": "event.office_bottom", "load_entity_id": "light.office"})
    assert result["type"] == "create_entry"
    control = next(iter(entry.subentries.values()))
    assert control.unique_id == control.data["unique_id"]
    assert control.data["buttons"][0]["id"] == "top"
    assert set(control.data["buttons"][0]["actions"]) == {"KeyPressed", "KeyPressed2x"}
    assert set(control.data["buttons"][1]["actions"]) == {"KeyReleased"}
    assert control.data["load_entity_id"] == "light.office"
    assert control.data["buttons"][0]["actions"]["KeyPressed2x"]["press_count"] == 2
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_unknown_native_profile_requires_explicit_mapping(hass):
    entry = await install(hass)
    hass.states.async_set("event.unknown", "unknown", {"event_types": ["new_vendor_action"]})
    result = await control_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"top_entity_id": "event.unknown"})
    assert result["errors"] == {"top_entity_id": "no_supported_events"}
    assert not entry.subentries


@pytest.mark.asyncio
async def test_advanced_validation_mqtt_dependency_and_stable_edit_identity(hass):
    entry = await install(hass)
    result = await control_flow(hass, entry, "advanced")
    assert result["step_id"] == "advanced"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": "[]"})
    assert result["errors"] == {"base": "invalid_mapping"}
    mqtt = {"sources": [{"id": "mqtt", "type": "mqtt", "topic": "zigbee/paddle", "buttons": [{"id": "top", "label": "Top"}],
        "mappings": [{"button_id": "top", "match": {"action": "single"}, "gesture": {"kind": "press", "press_count": 1}}]}]}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": json.dumps(mqtt)})
    assert result["errors"] == {"base": "mqtt_not_configured"}
    valid = {"unique_id": "cannot_override_generated_identity", "buttons": [{"id": "center", "label": "Center", "source_entity_id": "event.remote",
        "actions": {"tap": {"kind": "press", "press_count": 1}}}]}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": json.dumps(valid)})
    assert result["type"] == "create_entry"
    control = next(iter(entry.subentries.values())); identity = control.unique_id
    assert identity != valid["unique_id"]
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"name": "Renamed Paddle", "source_type": "advanced"})
    valid["buttons"][0]["label"] = "Renamed Center"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": json.dumps(valid)})
    assert result["type"] == "abort"
    assert control.unique_id == identity == control.data["unique_id"]
    assert control.data["buttons"][0]["id"] == "center"
    assert control.title == "Renamed Paddle"
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_single_installation_adds_independent_controls(hass):
    entry = await install(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"
    for name in ("one", "two"):
        hass.states.async_set(f"event.{name}", "unknown", {"event_types": ["single"]})
        result = await control_flow(hass, entry)
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"top_entity_id": f"event.{name}"})
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
    assert len(entry.subentries) == 2
    assert len({control.unique_id for control in entry.subentries.values()}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,identity,match", [
    ("zwave_js", {"device_id": "office_zwave_device"}, {"scene": 1, "value": "KeyPressed2x"}),
    ("zha", {"device_id": "office_zha_device"}, {"command": "double"}),
    ("shared_native", {"source_entity_id": "event.shared_paddle"}, {"event_type": "double"}),
    ("bus", {"event_type": "filtered_vendor_action", "filters": '{"device_id":"office"}'}, {"action": "double"}),
])
async def test_guided_sources_save_exact_identity_and_explicit_capabilities(hass, kind, identity, match):
    entry = await install(hass)
    result = await control_flow(hass, entry, kind)
    assert result["step_id"] == "source"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], identity | {
        "buttons": '[{"id":"top","label":"Top"}]',
        "mappings": json.dumps([{"button_id": "top", "match": match, "gesture": {"kind": "press", "press_count": 2}}]),
    })
    assert result["type"] == "create_entry"
    control = next(iter(entry.subentries.values()))
    source = control.data["sources"][0]
    assert source["type"] == kind
    assert source["mappings"][0]["match"] == match
    assert len(source["mappings"]) == 1  # No invented single/triple/hold support.
    for key, value in identity.items():
        assert source[key] == (json.loads(value) if key == "filters" else value)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_mqtt_alias_cannot_bypass_dependency_validation(hass):
    entry = await install(hass)
    result = await control_flow(hass, entry, "advanced")
    config = {"sources": [{"id": "source_1", "type": "zigbee2mqtt", "topic": "zigbee/paddle",
        "buttons": [{"id": "top", "label": "Top"}],
        "mappings": [{"button_id": "top", "match": {"action": "double"}, "gesture": {"kind": "press", "press_count": 2}}]}]}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": json.dumps(config)})
    assert result["errors"] == {"base": "mqtt_not_configured"}
    assert not entry.subentries


@pytest.mark.asyncio
async def test_native_registry_identity_persists_and_editor_resolves_renamed_source(hass):
    entry = await install(hass)
    registry = er.async_get(hass)
    source = registry.async_get_or_create("event", "source_test", "office_top", suggested_object_id="office_top")
    hass.states.async_set(source.entity_id, "unknown", {"event_types": ["KeyPressed2x"]})
    result = await control_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"top_entity_id": source.entity_id})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    button = control.data["buttons"][0]
    assert button["source_registry_id"] == source.id
    original_control_id = control.unique_id
    renamed = registry.async_update_entity(source.entity_id, new_entity_id="event.renamed_office_top")
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"name": "Office Paddle", "source_type": "advanced"})
    marker = next(key for key in result["data_schema"].schema if key.schema == "configuration")
    preview = json.loads(marker.default())
    assert preview["buttons"][0]["source_entity_id"] == renamed.entity_id
    assert preview["buttons"][0]["source_registry_id"] == source.id
    # Even old text in a submitted JSON configuration resolves through its
    # already captured UUID, preserving the button/control identities.
    preview["buttons"][0]["source_entity_id"] = source.entity_id
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"configuration": json.dumps(preview)})
    assert result["type"] == "abort"
    assert control.data["buttons"][0]["source_entity_id"] == renamed.entity_id
    assert control.unique_id == original_control_id
    assert control.data["buttons"][0]["id"] == "top"
    await hass.async_block_till_done()
