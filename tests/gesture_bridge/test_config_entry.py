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
from homeassistant.helpers import entity_registry as er
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
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": import_yaml})
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


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
async def test_import_yaml_mqtt_validation_retains_selector_and_retries(hass, monkeypatch):
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
    entry = await create_installation(hass)
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"name": "Import", "source_type": "import_yaml"})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"control": "existing_mqtt"})
    assert flow["type"] == "form" and flow["errors"] == {"base": "mqtt_not_configured"}
    field = next(iter(flow["data_schema"].schema))
    assert field.schema == "control" and field.default() == "existing_mqtt"
    assert flow["data_schema"].schema[field].config["options"] == [
        {"value": "existing_mqtt", "label": "Existing MQTT"}]
    assert not entry.subentries and entry.data["imported_yaml"] == []
    assert registry.async_get(entity_id).config_entry_id is None

    # Only the broker boundary is replaced; the flow, entity migration, registry
    # ownership and config-entry reload below all use HA's actual machinery.
    hass.config.components.add("mqtt")
    monkeypatch.setattr(mqtt, "is_connected", lambda _: False)
    subscriptions = []
    async def subscribe(hass, topic, callback, qos=0):
        subscriptions.append(topic)
        return lambda: None
    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"control": "existing_mqtt"})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1 and entry.data["imported_yaml"] == ["existing_mqtt"]
    assert registry.async_get_entity_id("event", DOMAIN, "existing_mqtt") == entity_id
    assert registry.async_get(entity_id).config_entry_id == entry.entry_id
    assert subscriptions == ["remote/action"]


async def test_relabel_and_reorder_keep_registered_control_and_gesture_identities(hass, monkeypatch):
    now, ticks = dt_util.utcnow(), count()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(milliseconds=next(ticks)))
    for source in ("event.upper", "event.lower"):
        hass.states.async_set(source, "unknown", {"event_types": ["KeyPressed2x"]})
    entry = await create_installation(hass)
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"name": "Office", "source_type": "native_event"})
    await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "top_entity_id": "event.upper", "bottom_entity_id": "event.lower"})
    await hass.async_block_till_done()
    control = next(iter(entry.subentries.values()))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, control.unique_id)
    registered_id = registry.async_get(entity_id).id
    hass.states.async_set("event.upper", dt_util.utcnow().isoformat(), {"event_type": "KeyPressed2x"})
    await hass.async_block_till_done()
    original = hass.states.get(entity_id)
    assert original.attributes["gesture_sequence"] == 1

    changed = deepcopy(dict(control.data))
    changed["buttons"].reverse()
    for button in changed["buttons"]:
        button["label"] = "Lower Paddle" if button["id"] == "bottom" else "Upper Paddle"
        button["actions"]["KeyPressed2x"]["label"] = "Scene Double Tap"
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": control.subentry_id})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {
        "name": "Renamed Office", "source_type": "advanced"})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"configuration": json.dumps(changed)})
    assert flow["type"] == "abort"
    await hass.async_block_till_done()
    current = hass.states.get(entity_id)
    validate_state(current.as_dict())
    assert registry.async_get_entity_id("event", DOMAIN, control.unique_id) == entity_id
    assert registry.async_get(entity_id).id == registered_id
    assert current.attributes["friendly_name"] == "Renamed Office"
    assert current.attributes["bridge_control_id"] == original.attributes["bridge_control_id"]
    assert current.attributes["bridge_update_kind"] == "snapshot"
    assert current.state == original.state
    assert current.attributes["gesture_event_id"] == original.attributes["gesture_event_id"]
    assert current.attributes["bridge_session_id"] != original.attributes["bridge_session_id"]
    assert [(button["id"], button["label"]) for button in current.attributes["bridge_buttons"]] == [
        ("bottom", "Lower Paddle"), ("top", "Upper Paddle")]
    assert all(button["gestures"] == [{"id": "press_2", "kind": "press", "label": "Scene Double Tap", "press_count": 2}]
               for button in current.attributes["bridge_buttons"])

    hass.states.async_set("event.lower", dt_util.utcnow().isoformat(), {"event_type": "KeyPressed2x"})
    await hass.async_block_till_done()
    fresh = hass.states.get(entity_id)
    validate_state(fresh.as_dict())
    assert fresh.attributes["button_id"] == "bottom" and fresh.attributes["gesture_id"] == "press_2"
    assert fresh.attributes["gesture_sequence"] == 1
    assert fresh.attributes["gesture_session_id"] == current.attributes["bridge_session_id"]


async def test_multiple_live_controls_keep_gestures_holds_loads_and_status_independent(hass, monkeypatch):
    now, ticks = dt_util.utcnow(), count()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(milliseconds=next(ticks)))
    entry = await create_installation(hass)
    for name in ("one", "two", "three"):
        hass.states.async_set(f"event.{name}", "unknown", {
            "event_types": ["KeyPressed2x", "KeyHeldDown", "KeyReleased"]})
        hass.states.async_set(f"switch.{name}", "off")
        flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
        flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"name": name, "source_type": "native_event"})
        await hass.config_entries.subentries.async_configure(flow["flow_id"], {
            "top_entity_id": f"event.{name}", "load_entity_id": f"switch.{name}"})
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    identities = {control.title: registry.async_get_entity_id("event", DOMAIN, control.unique_id)
                  for control in entry.subentries.values()}

    def states():
        current = {name: hass.states.get(entity_id) for name, entity_id in identities.items()}
        for state in current.values():
            validate_state(state.as_dict())
        return current

    initial = states()
    assert len({state.attributes["bridge_session_id"] for state in initial.values()}) == 3
    assert all("gesture_sequence" not in state.attributes for state in initial.values())
    for action in ("KeyHeldDown", "KeyHeldDown"):
        hass.states.async_set("event.one", dt_util.utcnow().isoformat(), {"event_type": action})
        await hass.async_block_till_done()
    first = states()
    assert first["one"].attributes["gesture_sequence"] == 2
    assert first["one"].attributes["event_type"] == "hold_repeat"
    assert first["two"] is initial["two"] and first["three"] is initial["three"]
    for name in ("two", "three"):
        hass.states.async_set(f"event.{name}", dt_util.utcnow().isoformat(), {"event_type": "KeyPressed2x"})
        await hass.async_block_till_done()
    before = states()
    assert before["one"] is first["one"]
    assert before["two"].attributes["gesture_sequence"] == before["three"].attributes["gesture_sequence"] == 1
    assert len({state.attributes["gesture_event_id"] for state in before.values()}) == 3

    hass.states.async_set("switch.one", "unavailable")
    hass.states.async_set("event.two", "unavailable")
    await hass.async_block_till_done()
    changed = states()
    assert changed["one"].attributes["bridge_load"]["available"] is False
    assert changed["one"].attributes["bridge_sources"][0]["ready"] is True
    assert changed["two"].attributes["bridge_sources"][0]["ready"] is False
    assert changed["two"].attributes["bridge_load"]["available"] is True
    assert changed["three"] is before["three"]
    assert all(changed[name].attributes["gesture_event_id"] == state.attributes["gesture_event_id"]
               and changed[name].state == state.state for name, state in before.items())
    hass.states.async_set("event.one", dt_util.utcnow().isoformat(), {"event_type": "KeyReleased"})
    await hass.async_block_till_done()
    final = states()
    assert final["one"].attributes["gesture_sequence"] == 3
    assert final["one"].attributes["event_type"] == "release"
    assert final["one"].attributes["bridge_load"]["available"] is False
    assert final["two"] is changed["two"] and final["three"] is changed["three"]
