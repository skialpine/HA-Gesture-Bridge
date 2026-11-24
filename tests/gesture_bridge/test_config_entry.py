"""Actual HA platform lifecycle, reload, removal and YAML ownership migration."""

from copy import deepcopy
from datetime import timedelta
from itertools import count
import json
import logging

from types import MappingProxyType

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge import async_setup_entry
from custom_components.ha_gesture_bridge.diagnostics import async_get_config_entry_diagnostics
from tools.validate_gesture_contract import validate_state

YAML_CONTROL = {"platform": DOMAIN, "unique_id": "office_existing", "name": "Existing Office Paddle",
    "source_entity_id": "event.office_source", "button_id": "top", "button_label": "Top",
    "actions": {"KeyPressed": {"kind": "press", "press_count": 1}}}


async def create_installation(hass, import_yaml=False):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": True} if import_yaml else {})
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


def source_device(hass, entry):
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_paddles", "desk")}, name="Desk Paddle")
    entity = er.async_get(hass).async_get_or_create("event", "fixture_paddles", "desk-upper",
        device_id=device.id, original_name="Upper paddle", suggested_object_id="fixture_desk_upper")
    hass.states.async_set(entity.entity_id, "unknown", {"event_types": ["KeyPressed"]})
    return device


def review_defaults(flow):
    return flow["data_schema"]({})


@pytest.mark.asyncio
async def test_yaml_platform_metadata_import_keeps_entity_identity_and_friendly_editing(hass):
    hass.states.async_set(YAML_CONTROL["source_entity_id"], "unknown", {"event_types": ["KeyPressed"]})
    platform_config = deepcopy(YAML_CONTROL) | {"entity_namespace": "legacy", "scan_interval": timedelta(seconds=30)}
    assert await async_setup_component(hass, "event", {"event": [platform_config]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, YAML_CONTROL["unique_id"])
    assert entity_id is not None
    entry = await create_installation(hass, import_yaml=True)
    control = next(iter(entry.subentries.values()))
    assert registry.async_get_entity_id("event", DOMAIN, control.unique_id) == entity_id
    assert not {"platform", "entity_namespace", "scan_interval"} & control.data.keys()
    original = hass.data[DOMAIN]["yaml_controls"][control.unique_id]["config"]
    assert original["scan_interval"] == timedelta(seconds=30)
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    device = source_device(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"device_id": device.id})
    assert result["step_id"] == "review" and "configuration" not in review_defaults(result)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], review_defaults(result))
    assert not {"platform", "entity_namespace", "scan_interval"} & control.data.keys()
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("event", DOMAIN, control.unique_id) == entity_id


@pytest.mark.asyncio
async def test_import_loaded_yaml_preserves_entity_identity_and_single_listener(hass, monkeypatch):
    instant = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: instant[0])

    class OfficePaddle(EventEntity):
        _attr_name = "Office Source"
        _attr_unique_id = "office_source_fixture"
        _attr_event_types = ["KeyPressed"]

        async def press(self):
            # Advance the actual native event timestamp beyond each decoder's
            # initialization/watermark without a real sleep or an old fixture.
            instant[0] += timedelta(milliseconds=100)
            self._trigger_event("KeyPressed")
            self.async_write_ha_state()
            await self.hass.async_block_till_done()

    source_component = EntityComponent(logging.getLogger(__name__), "event", hass)
    source = OfficePaddle()
    await source_component.async_add_entities([source])
    source_registry = er.async_get(hass).async_get(source.entity_id)
    assert source.entity_id == YAML_CONTROL["source_entity_id"] and source_registry is not None
    assert await async_setup_component(hass, "event", {"event": [deepcopy(YAML_CONTROL)]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, "office_existing")
    legacy = hass.data[DOMAIN]["yaml_controls"]["office_existing"]["entity"]
    await source.press()
    assert legacy._sequence == 1
    assert hass.states.get(entity_id).attributes["gesture_sequence"] == 1
    entry = await create_installation(hass, import_yaml=True)
    control = next(iter(entry.subentries.values()))
    assert control.unique_id == "office_existing"
    assert control.data["source_registry_id"] == source_registry.id
    assert entry.data["imported_yaml"] == ["office_existing"]
    assert registry.async_get_entity_id("event", DOMAIN, "office_existing") == entity_id
    assert registry.async_get(entity_id).config_entry_id == entry.entry_id
    assert registry.async_get(entity_id).config_subentry_id == control.subentry_id
    imported = hass.data[DATA_INSTANCES]["event"].get_entity(entity_id)
    assert imported is not None and imported is not legacy
    assert len([state for state in hass.states.async_all("event")
                if state.attributes.get("bridge_control_id") == "office_existing"]) == 1
    await source.press()
    assert hass.states.get(entity_id).attributes["gesture_sequence"] == imported._sequence == 1
    assert legacy._sequence == 1  # The removed YAML callback must not run.
    first_session = hass.states.get(entity_id).attributes["bridge_session_id"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = hass.data[DATA_INSTANCES]["event"].get_entity(entity_id)
    assert reloaded is not None and reloaded is not imported
    assert hass.states.get(entity_id).attributes["bridge_session_id"] != first_session
    assert len([state for state in hass.states.async_all("event")
                if state.attributes.get("bridge_control_id") == "office_existing"]) == 1
    await source.press()
    assert hass.states.get(entity_id).attributes["gesture_sequence"] == reloaded._sequence == 1
    assert legacy._sequence == imported._sequence == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(entity_id)
    await source.press()  # Fresh enough to execute any leaked callback.
    assert hass.states.get(entity_id) == before
    assert legacy._sequence == imported._sequence == reloaded._sequence == 1


@pytest.mark.asyncio
async def test_add_edit_remove_controls_reload_actual_event_platform(hass):
    entry = await create_installation(hass)
    device = source_device(hass, entry)
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
    await hass.config_entries.subentries.async_configure(flow["flow_id"], review_defaults(flow) | {"name": "Desk Paddle"})
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, control.unique_id)
    assert entity_id and hass.states.get(entity_id) is not None
    initial = hass.states.get(entity_id).attributes["bridge_session_id"]
    changed = dict(control.data) | {"name": "Renamed Desk"}
    hass.config_entries.async_update_subentry(entry, control, title="Renamed Desk", data=changed)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("event", DOMAIN, control.unique_id) == entity_id
    assert hass.states.get(entity_id).attributes["bridge_session_id"] != initial
    hass.config_entries.async_remove_subentry(entry, control.subentry_id)
    await hass.async_block_till_done()
    assert not entry.subentries
    assert registry.async_get(entity_id) is None
    assert not hass.states.get(entity_id) or hass.states.get(entity_id).state == "unavailable"
    assert not [state for state in hass.states.async_all("event")
                if state.attributes.get("bridge_control_id") == control.unique_id]


@pytest.mark.asyncio
async def test_initial_import_keeps_existing_yaml_source_identity(hass):
    assert await async_setup_component(hass, "event", {"event": [deepcopy(YAML_CONTROL)]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    original_id = registry.async_get_entity_id("event", DOMAIN, "office_existing")
    entry = await create_installation(hass, import_yaml=True)
    assert len(entry.subentries) == 1
    assert registry.async_get_entity_id("event", DOMAIN, "office_existing") == original_id
    assert registry.async_get(original_id).config_entry_id == entry.entry_id


@pytest.mark.asyncio
async def test_invalid_stored_control_cannot_partially_remove_working_yaml(hass):
    assert await async_setup_component(hass, "event", {"event": [deepcopy(YAML_CONTROL)]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    original_id = registry.async_get_entity_id("event", DOMAIN, "office_existing")
    before = hass.states.get(original_id)
    entry = ConfigEntry(domain=DOMAIN, title="HA Gesture Bridge", data={}, options={}, version=1, minor_version=1,
        source=SOURCE_USER, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[
            {"data": deepcopy(YAML_CONTROL), "subentry_type": "control", "title": "Working YAML", "unique_id": "office_existing"},
            {"data": {"unique_id": "broken", "name": "Broken"}, "subentry_type": "control", "title": "Broken", "unique_id": "broken"},
        ])
    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, entry)
    assert hass.states.get(original_id) == before
    assert registry.async_get(original_id).config_entry_id is None


@pytest.mark.asyncio
async def test_imported_control_diagnostics_follow_current_ui_not_retained_yaml(hass):
    hass.states.async_set("event.office_source", "unknown", {"event_types": ["KeyPressed"]})
    hass.states.async_set("event.office_lower", "unknown", {"event_types": ["KeyPressed"]})
    assert await async_setup_component(hass, "event", {"event": [deepcopy(YAML_CONTROL)]})
    await hass.async_block_till_done()
    entry = await create_installation(hass, import_yaml=True)
    control = next(iter(entry.subentries.values()))
    updated = {"unique_id": control.unique_id, "name": "Edited Office", "load_entity_id": "light.office",
        "buttons": [{"id": bid, "label": bid.title(), "source_entity_id": source,
                     "actions": {"KeyPressed": {"kind": "press", "press_count": 1}}}
                    for bid, source in (("top", "event.office_source"), ("bottom", "event.office_lower"))]}
    hass.config_entries.async_update_subentry(entry, control, title="Edited Office", data=updated)
    await hass.async_block_till_done()
    assert "buttons" not in hass.data[DOMAIN]["yaml_controls"][control.unique_id]["config"]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["configuration"] == {"controls": 1, "sources": 2,
        "source_types": {"native_event": 2}, "linked_load_domains": {"light": 1}, "invalid_controls": 0}
    assert diagnostics["runtime"]["aggregates"] == 1
    assert diagnostics["runtime"]["ready_sources"] == 2


@pytest.mark.asyncio
async def test_initial_yaml_mqtt_validation_retries_without_partial_migration(hass, monkeypatch):
    from homeassistant.components import mqtt

    data = {"platform": DOMAIN, "unique_id": "existing_mqtt", "name": "Existing MQTT",
        "sources": [{"id": "remote", "type": "mqtt", "topic": "remote/action",
            "buttons": [{"id": "top", "label": "Top"}], "mappings": [
                {"button_id": "top", "match": {"action": "double"},
                 "gesture": {"kind": "press", "press_count": 2}}]}]}
    assert await async_setup_component(hass, "event", {"event": [data]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, "existing_mqtt")
    assert entity_id and hass.states.get(entity_id) is not None
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"import_yaml": True})
    assert flow["type"] == "form" and flow["errors"] == {"base": "mqtt_not_configured"}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert registry.async_get(entity_id).config_entry_id is None
    hass.config.components.add("mqtt")
    monkeypatch.setattr(mqtt, "is_connected", lambda _: False)
    subscriptions = []
    async def subscribe(hass, topic, callback, qos=0):
        subscriptions.append(topic)
        return lambda: None
    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"import_yaml": True})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    entry = flow["result"]
    assert len(entry.subentries) == 1 and entry.data["imported_yaml"] == ["existing_mqtt"]
    assert registry.async_get_entity_id("event", DOMAIN, "existing_mqtt") == entity_id
    assert registry.async_get(entity_id).config_entry_id == entry.entry_id
    assert subscriptions == ["remote/action"]
