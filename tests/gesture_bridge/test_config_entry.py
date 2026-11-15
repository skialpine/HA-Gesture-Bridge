"""Actual HA platform lifecycle, reload, removal and YAML ownership migration."""

from copy import deepcopy

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_USER
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge import async_setup_entry

YAML_CONTROL = {"platform": DOMAIN, "unique_id": "office_existing", "name": "Existing Office Paddle",
    "source_entity_id": "event.office_source", "button_id": "top", "button_label": "Top",
    "actions": {"KeyPressed": {"kind": "press", "press_count": 1}}}


async def create_installation(hass, import_yaml=False):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": import_yaml})
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.mark.asyncio
async def test_import_loaded_yaml_preserves_entity_identity_and_single_listener(hass):
    source = er.async_get(hass).async_get_or_create("event", "source_test", "office_source", suggested_object_id="office_source")
    hass.states.async_set("event.office_source", "unknown", {"event_types": ["KeyPressed"]})
    assert await async_setup_component(hass, "event", {"event": [deepcopy(YAML_CONTROL)]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, "office_existing")
    assert entity_id
    assert hass.states.get(entity_id).attributes["bridge_control_id"] == "office_existing"
    entry = await create_installation(hass, import_yaml=True)
    control = next(iter(entry.subentries.values()))
    assert control.unique_id == "office_existing"
    assert control.data["source_registry_id"] == source.id
    assert entry.data["imported_yaml"] == ["office_existing"]
    assert registry.async_get_entity_id("event", DOMAIN, "office_existing") == entity_id
    assert registry.async_get(entity_id).config_entry_id == entry.entry_id
    assert registry.async_get(entity_id).config_subentry_id == control.subentry_id
    assert hass.states.get(entity_id) is not None
    assert len([state for state in hass.states.async_all("event")
                if state.attributes.get("bridge_control_id") == "office_existing"]) == 1
    first_session = hass.states.get(entity_id).attributes["bridge_session_id"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["bridge_session_id"] != first_session
    assert len([state for state in hass.states.async_all("event")
                if state.attributes.get("bridge_control_id") == "office_existing"]) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # No state writer remains: source changes cannot recreate/update a removed
    # control after its config entry and platform listeners have unloaded.
    before = hass.states.get(entity_id)
    hass.states.async_set("event.office_source", "2026-11-14T20:00:01Z", {"event_type": "KeyPressed"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) == before


@pytest.mark.asyncio
async def test_add_edit_remove_controls_reload_actual_event_platform(hass):
    entry = await create_installation(hass)
    hass.states.async_set("event.source", "unknown", {"event_types": ["single"]})
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"name": "Desk Paddle", "source_type": "native_event"})
    await hass.config_entries.subentries.async_configure(flow["flow_id"], {"top_entity_id": "event.source"})
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
async def test_import_another_yaml_control_from_existing_installation(hass):
    assert await async_setup_component(hass, "event", {"event": [deepcopy(YAML_CONTROL)]})
    await hass.async_block_till_done()
    entry = await create_installation(hass)
    registry = er.async_get(hass)
    original_id = registry.async_get_entity_id("event", DOMAIN, "office_existing")
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"name": "Ignored import name", "source_type": "import_yaml"})
    assert flow["step_id"] == "import_yaml"
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"control": "office_existing"})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
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
