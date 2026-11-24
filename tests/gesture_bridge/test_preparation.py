"""First-use enrollment through real HA services, registries and reloads."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig
from custom_components.ha_gesture_bridge.device_catalog import device_choices
from custom_components.ha_gesture_bridge.preparation import AUTOMATIC, runtime_config
from test_config_flow import install, device_fixture, start_control, default_input


async def prepare(hass, device, endpoint=None, context=None):
    data = {"device_id": device.id}
    if endpoint:
        data["source_registry_id"] = endpoint.id
    return await hass.services.async_call(DOMAIN, "prepare_device", data,
        blocking=True, return_response=True, context=context)


def relay(hass, device, name="relay", domain="switch", platform="fixture"):
    item = er.async_get(hass).async_get_or_create(domain, platform, name, device_id=device.id,
        suggested_object_id=name, original_name=name,
        capabilities={"supported_color_modes": ["brightness"]} if domain == "light" else None)
    hass.states.async_set(item.entity_id, "off", {"supported_color_modes": ["brightness"]} if domain == "light" else {})
    return item


async def test_catalog_is_read_only_and_retains_distinct_endpoints(hass, monkeypatch):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    a, b = relay(hass, device), relay(hass, device, "dimmer", "light")
    remote, _ = device_fixture(hass, entry, "Remote")
    hidden = relay(hass, device, "c4_export", platform="control4")
    disabled = relay(hass, device, "disabled")
    er.async_get(hass).async_update_entity(disabled.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    forbidden = AsyncMock(side_effect=AssertionError("Browsing queried a provider"))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", forbidden)
    for _ in range(2):
        choices = device_choices(hass)
        own = [row for row in choices if row["device_id"] == device.id]
        assert {row["source_load"]["registry_id"] for row in own} == {a.id, b.id}
        assert any(row["device_id"] == remote.id and row["kind"] == "remote" for row in choices)
        assert not any(row.get("source_load", {}).get("entity_id") in {hidden.entity_id, disabled.entity_id} for row in choices)
    assert not entry.subentries and forbidden.await_count == 0
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.ha_gesture_bridge_catalog").attributes
    assert attrs["catalog_devices_total"] == 3 and attrs["catalog_devices_pages"] == 1


async def test_first_use_concurrency_restart_and_pinned_missing_native_state(hass, monkeypatch):
    entry = await install(hass)
    device, sources = device_fixture(hass, entry)
    endpoint = relay(hass, device)
    first, second = await asyncio.gather(prepare(hass, device, endpoint), prepare(hass, device, endpoint))
    await hass.async_block_till_done()
    assert first["gesture_entity_id"] == second["gesture_entity_id"] and first["input_ready"]
    assert len(entry.subentries) == 1
    sub = next(iter(entry.subentries.values()))
    assert AUTOMATIC in sub.data and "buttons" not in sub.data and "sources" not in sub.data
    identity = first["gesture_entity_id"]
    original = deepcopy(hass.states.get(identity).attributes["bridge_buttons"])
    # A generic trigger fallback must not change the native binding at restart.
    forbidden = AsyncMock(side_effect=AssertionError("Pinned source fell through to triggers"))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", forbidden)
    hass.states.async_remove(sources[0].entity_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(identity)
    assert state.attributes["bridge_buttons"] == original
    missing_role = next(binding["role"] for binding in sub.data[AUTOMATIC]["bindings"]
                        if binding.get("source_registry_id") == sources[0].id)
    assert "gesture_sequence" not in state.attributes
    assert not next(item for item in state.attributes["bridge_sources"] if item["button_id"] == missing_role)["ready"]
    assert len(state.attributes["bridge_sources"]) == 2 and forbidden.await_count == 0
    fresh = dt_util.utcnow().isoformat()
    hass.states.async_set(sources[0].entity_id, fresh, {"event_types": ["KeyPressed", "KeyPressed2x"], "event_type": "KeyPressed"})
    await hass.async_block_till_done()
    # First returning state is a baseline, not a fresh physical press.
    assert "gesture_sequence" not in hass.states.get(identity).attributes
    again = await prepare(hass, device, endpoint)
    assert again["gesture_entity_id"] == identity and len(entry.subentries) == 1


async def test_preparation_resolves_selected_endpoint_after_enrollment(hass, monkeypatch):
    from custom_components.ha_gesture_bridge.preparation import Preparation

    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    endpoint = relay(hass, device)
    service = Preparation(hass)

    async def enroll(_):
        renamed = er.async_get(hass).async_update_entity(endpoint.entity_id, new_entity_id="switch.renamed")
        hass.states.async_set(renamed.entity_id, "on")
        hass.states.async_set(endpoint.entity_id, "off")
        return None

    monkeypatch.setattr(service, "_enroll", enroll)
    prepared = await service.prepare(device.id, endpoint.id)
    for field in ("source_load", "controlled_load"):
        assert prepared[field] == {"entity_id": "switch.renamed", "registry_id": endpoint.id,
                                   "domain": "switch", "available": True}


async def test_manual_setup_during_preparation_reuses_its_control(hass, monkeypatch):
    import custom_components.ha_gesture_bridge.preparation as preparation

    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    discover = preparation.discover_device

    async def delayed_discovery(hass, device_id):
        result = await discover(hass, device_id)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(preparation, "discover_device", delayed_discovery)
    pending = asyncio.create_task(prepare(hass, device))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        form = await start_control(hass, entry, device)
        saved = await hass.config_entries.subentries.async_configure(form["flow_id"], default_input(form))
        assert saved["type"] == "create_entry" and len(entry.subentries) == 1
    finally:
        release.set()
    result = await asyncio.wait_for(pending, 25)
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1
    control = next(iter(entry.subentries.values()))
    assert result["control_id"] == control.unique_id
    assert result["gesture_entity_id"] == er.async_get(hass).async_get_entity_id("event", DOMAIN, control.unique_id)
    again = await prepare(hass, device)
    assert again["control_id"] == result["control_id"] and len(entry.subentries) == 1


async def test_temporary_trigger_provider_failure_reports_source_unavailable(hass, monkeypatch):
    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_remote", "temporary")}, name="Remote", model="Remote")
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations",
                        AsyncMock(side_effect=HomeAssistantError("provider temporarily unavailable")))
    with pytest.raises(ServiceValidationError, match="^source_metadata_unavailable$"):
        await prepare(hass, device)
    assert not entry.subentries


async def test_load_only_and_cross_device_endpoint(hass):
    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture", "load")}, name="Load only")
    endpoint = relay(hass, device)
    result = await prepare(hass, device, endpoint)
    assert "gesture_entity_id" not in result and not entry.subentries
    assert result["controlled_load_origin"] == "selected_endpoint"
    other, _ = device_fixture(hass, entry)
    with pytest.raises(ServiceValidationError, match="invalid_endpoint"):
        await prepare(hass, other, endpoint)


@pytest.mark.parametrize("registry_change", [
    {"disabled_by": er.RegistryEntryDisabler.USER},
    {"hidden_by": er.RegistryEntryHider.USER},
])
async def test_ineligible_aggregate_leaves_relay_available_without_gestures(hass, registry_change):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    endpoint = relay(hass, device)
    enrolled = await prepare(hass, device, endpoint)
    event = enrolled["gesture_entity_id"]
    assert enrolled["input_ready"]
    await hass.async_block_till_done()

    er.async_get(hass).async_update_entity(event, **registry_change)
    await hass.async_block_till_done()
    picker = await hass.services.async_call(DOMAIN, "get_devices", {}, blocking=True, return_response=True)
    choice = next(row["attributes"]["device_choice"] for row in picker["entities"]
                  if row["entity_id"] == f"device:{device.id}:{endpoint.id}")
    result = await prepare(hass, device, endpoint)
    assert choice["gestures"] == "candidate" and "gesture_entity_id" not in choice
    assert "gesture_entity_id" not in result and "control_id" not in result
    assert result["input_ready"] is False
    assert result["source_load"] == result["controlled_load"] == enrolled["source_load"]
    assert result["controlled_load_origin"] == "selected_endpoint"
    assert len(entry.subentries) == 1


async def test_disabled_button_only_aggregate_is_unavailable(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    enrolled = await prepare(hass, device)
    await hass.async_block_till_done()
    er.async_get(hass).async_update_entity(enrolled["gesture_entity_id"], disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()

    assert not any(choice["device_id"] == device.id for choice in device_choices(hass))
    with pytest.raises(ServiceValidationError, match="^device_unavailable$"):
        await prepare(hass, device)


async def test_extended_device_identifiers_do_not_break_catalog_or_preparation(hass):
    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture", "extended", "endpoint")}, name="Extended identifier")
    endpoint = relay(hass, device)
    exported = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("control4", "exported", "proxy")}, name="Existing C4 light")
    relay(hass, exported, "exported_light")
    choices = device_choices(hass)
    assert any(row["device_id"] == device.id for row in choices)
    assert not any(row["device_id"] == exported.id for row in choices)
    result = await prepare(hass, device, endpoint)
    assert result["controlled_load"]["entity_id"] == endpoint.entity_id
    with pytest.raises(ServiceValidationError, match="device_unavailable"):
        await prepare(hass, exported)


async def test_explicit_link_wins_and_deleted_target_never_falls_back(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    endpoint = relay(hass, device)
    bulb = er.async_get(hass).async_get_or_create("light", "fixture", "bulb", suggested_object_id="bulb")
    hass.states.async_set(bulb.entity_id, "on", {"supported_color_modes": ["brightness"]})
    form = await start_control(hass, entry, device)
    await hass.config_entries.subentries.async_configure(form["flow_id"], default_input(form) | {"load_entity_id": bulb.entity_id})
    await hass.async_block_till_done()
    sub = next(iter(entry.subentries.values()))
    assert sub.data["load_registry_id"] == bulb.id
    original_id = sub.unique_id
    result = await prepare(hass, device, endpoint)
    await hass.async_block_till_done()
    assert result["control_id"] == original_id
    assert result["source_load"]["entity_id"] == endpoint.entity_id
    assert result["controlled_load"]["entity_id"] == bulb.entity_id
    assert result["controlled_load_origin"] == "explicit_link"
    registry = er.async_get(hass)
    registry.async_update_entity(result["gesture_entity_id"], hidden_by=er.RegistryEntryHider.USER)
    without_gestures = await prepare(hass, device, endpoint)
    assert "gesture_entity_id" not in without_gestures and "control_id" not in without_gestures
    assert without_gestures["controlled_load"] == result["controlled_load"]
    assert without_gestures["controlled_load_origin"] == "explicit_link"
    registry.async_update_entity(result["gesture_entity_id"], hidden_by=None)
    old_entity_id = bulb.entity_id
    bulb = registry.async_update_entity(bulb.entity_id, new_entity_id="light.renamed_bulb")
    hass.states.async_set(bulb.entity_id, "on", {"supported_color_modes": ["brightness"]})
    hass.states.async_set(old_entity_id, "off")
    await hass.async_block_till_done()
    snapshot = hass.states.get(result["gesture_entity_id"]).attributes["bridge_load"]
    assert (snapshot["entity_id"], snapshot["state"]) == (bulb.entity_id, "on")
    renamed = await prepare(hass, device, endpoint)
    assert renamed["controlled_load"]["entity_id"] == snapshot["entity_id"]
    er.async_get(hass).async_update_entity(bulb.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    disabled = await prepare(hass, device, endpoint)
    assert disabled["controlled_load"]["available"] is False
    assert "registry_id" not in disabled["controlled_load"]
    er.async_get(hass).async_update_entity(bulb.entity_id, disabled_by=None)
    er.async_get(hass).async_remove(bulb.entity_id)
    replacement = registry.async_get_or_create("light", "fixture", "replacement", suggested_object_id="renamed_bulb")
    assert replacement.id != bulb.id
    hass.states.async_set(replacement.entity_id, "on")
    await hass.async_block_till_done()
    snapshot = hass.states.get(result["gesture_entity_id"]).attributes["bridge_load"]
    assert not snapshot["available"]
    result = await prepare(hass, device, endpoint)
    assert result["controlled_load"]["entity_id"] == old_entity_id
    assert result["controlled_load"]["available"] is False
    assert result["controlled_load_origin"] == "explicit_link"



async def test_non_admin_denied_before_inspection_or_existing_lookup(hass, monkeypatch):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
    forbidden = AsyncMock(side_effect=AssertionError("Authorization came after inspection"))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.preparation.discover_device", forbidden)
    with pytest.raises(Unauthorized):
        await prepare(hass, device, context=Context(user_id="ordinary-user"))
    assert not entry.subentries and forbidden.await_count == 0
    hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=True)
    monkeypatch.undo()
    await prepare(hass, device, context=Context(user_id="admin-user"))
    hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=False)
    with pytest.raises(Unauthorized):
        await prepare(hass, device, context=Context(user_id="ordinary-user"))
    assert len(entry.subentries) == 1


async def test_removed_pinned_source_does_not_bind_name_reuse(hass):
    entry = await install(hass)
    device, sources = device_fixture(hass, entry)
    result = await prepare(hass, device)
    original = sources[0]
    er.async_get(hass).async_remove(original.entity_id)
    er.async_get(hass).async_get_or_create("event", "fixture_paddles", "replacement",
        device_id=device.id, suggested_object_id=original.entity_id.split(".")[1])
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    sub = next(iter(entry.subentries.values()))
    parsed = NativeControlConfig.parse(runtime_config(hass, sub.data))
    bound = next(item for item in parsed.inputs if item.source_registry_id == original.id)
    assert not next(item for item in hass.states.get(result["gesture_entity_id"]).attributes["bridge_sources"]
                    if item["button_id"] == bound.id)["ready"]


async def test_automatic_customization_keeps_recipe_and_clearing_link_restores_endpoint(hass):
    from homeassistant.config_entries import SOURCE_RECONFIGURE

    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    endpoint = relay(hass, device)
    first = await prepare(hass, device, endpoint)
    sub = next(iter(entry.subentries.values()))
    bulb = er.async_get(hass).async_get_or_create("light", "fixture", "linked", suggested_object_id="linked")
    hass.states.async_set(bulb.entity_id, "off")
    for target in (bulb.entity_id, None):
        flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"),
            context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id})
        form = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
        values = default_input(form)
        if target:
            values["load_entity_id"] = target
        else:
            values.pop("load_entity_id", None)
        saved = await hass.config_entries.subentries.async_configure(form["flow_id"], values)
        assert saved["type"] == "abort", saved
        await hass.async_block_till_done()
        assert AUTOMATIC in sub.data and "buttons" not in sub.data
        result = await prepare(hass, device, endpoint)
        assert result["gesture_entity_id"] == first["gesture_entity_id"]
        assert result["controlled_load"]["entity_id"] == (target or endpoint.entity_id)
        assert result["controlled_load_origin"] == ("explicit_link" if target else "selected_endpoint")


@pytest.mark.parametrize("model", ["ZEN26", "ZEN27", "ZEN71", "ZEN72", "ZEN76", "ZEN77"])
async def test_zooz_roles_use_native_value_identity_not_labels(hass, model):
    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("zwave_js", "1234-7")}, name="Paddle", manufacturer="Zooz", model=model)
    for scene in ("001", "002"):
        source = er.async_get(hass).async_get_or_create("event", "zwave_js", f"1234.7-91-0-scene-{scene}",
            device_id=device.id, original_name="Untranslated input", suggested_object_id="scene_" + scene)
        hass.states.async_set(source.entity_id, "unknown", {"event_types": ["KeyPressed", "KeyPressed2x"]})
    result = await prepare(hass, device)
    buttons = hass.states.get(result["gesture_entity_id"]).attributes["bridge_buttons"]
    assert {button["id"] for button in buttons} == {"top", "bottom"}


async def test_trigger_control_uses_one_actual_provider_attachment_and_pinned_path(hass, monkeypatch):
    from test_device_discovery import provider, trigger

    entry = await install(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_remote", "remote")}, name="Trigger Remote", model="Remote")
    calls, callbacks, detached, _ = provider(hass, monkeypatch, [
        trigger(device.id, "remote_button_short_press"), trigger(device.id, "remote_button_double_press")])
    first = await prepare(hass, device)
    assert len(calls) == 2
    again = await prepare(hass, device)
    assert first["gesture_entity_id"] == again["gesture_entity_id"] and len(calls) == 2
    await callbacks[0]({"trigger": {}})
    await hass.async_block_till_done()
    assert hass.states.get(first["gesture_entity_id"]).attributes["gesture_sequence"] == 1
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(detached) == 2 and len(calls) == 4
    after = hass.states.get(first["gesture_entity_id"]).attributes
    assert after["bridge_update_kind"] == "snapshot"
    assert after["bridge_session_id"] != after["gesture_session_id"]


async def test_device_projection_consolidates_wrapper_without_device_entry(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    endpoint = relay(hass, device)
    registry = er.async_get(hass)
    wrapper = registry.async_get_or_create("light", "switch_as_x", "wrapper", suggested_object_id="wrapper")
    registry.async_update_entity_options(wrapper.entity_id, "switch_as_x", {"entity_id": endpoint.id, "invert": False})
    hass.states.async_set(wrapper.entity_id, "off", {"supported_color_modes": ["onoff"]})
    choices = [row for row in device_choices(hass) if row["device_id"] == device.id]
    assert len(choices) == 1 and choices[0]["source_load"]["registry_id"] == wrapper.id
    prepared = await prepare(hass, device, endpoint)
    assert prepared["source_load"]["entity_id"] == wrapper.entity_id


async def test_unknown_five_button_layout_and_enrollment_during_hold(hass):
    entry = await install(hass)
    device, sources = device_fixture(hass, entry)
    first = await prepare(hass, device)
    hass.states.async_set(sources[0].entity_id, dt_util.utcnow().isoformat(),
        {"event_types": ["KeyPressed", "KeyHeldDown", "KeyReleased"], "event_type": "KeyHeldDown"})
    await hass.async_block_till_done()
    before = hass.states.get(first["gesture_entity_id"])
    assert before.attributes["event_type"] == "hold_start"
    remote, _ = device_fixture(hass, entry, "Five Inputs", [(str(i), ["single", "double"]) for i in range(5)])
    other = await prepare(hass, remote)
    await hass.async_block_till_done()
    buttons = hass.states.get(other["gesture_entity_id"]).attributes["bridge_buttons"]
    assert len({button["id"] for button in buttons}) == 5
    assert not {"top", "bottom", "toggle"}.intersection(button["id"] for button in buttons)
    after = hass.states.get(first["gesture_entity_id"])
    assert after.attributes["bridge_update_kind"] == "snapshot"
    assert after.attributes["bridge_session_id"] != before.attributes["bridge_session_id"]
    assert after.attributes["gesture_session_id"] == before.attributes["gesture_session_id"]
    assert after.state == before.state  # No synthetic release or replay on reload.


async def test_two_load_endpoints_share_gestures_without_rewriting_target(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    a, b = relay(hass, device, "load_a"), relay(hass, device, "load_b")
    first, second = await asyncio.gather(prepare(hass, device, a), prepare(hass, device, b))
    assert first["gesture_entity_id"] == second["gesture_entity_id"]
    assert first["controlled_load"]["entity_id"] == a.entity_id
    assert second["controlled_load"]["entity_id"] == b.entity_id
    assert "load_entity_id" not in next(iter(entry.subentries.values())).data


async def test_first_use_missing_native_metadata_does_not_choose_trigger_fallback(hass, monkeypatch):
    entry = await install(hass)
    device, sources = device_fixture(hass, entry)
    hass.states.async_remove(sources[0].entity_id)
    fallback = AsyncMock(side_effect=AssertionError("Missing metadata fell through to a trigger"))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", fallback)
    with pytest.raises(ServiceValidationError, match="source_metadata_unavailable"):
        await prepare(hass, device)
    assert not entry.subentries and fallback.await_count == 0


async def test_automatic_metadata_follows_source_rename_without_reload_on_press(hass, monkeypatch):
    entry = await install(hass)
    device, sources = device_fixture(hass, entry)
    prepared = await prepare(hass, device)
    await hass.async_block_till_done()
    original = deepcopy(next(iter(entry.subentries.values())).data[AUTOMATIC])
    reload_entry = AsyncMock(wraps=hass.config_entries.async_reload)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    renamed = er.async_get(hass).async_update_entity(sources[0].entity_id, new_entity_id="event.renamed_input")
    await hass.async_block_till_done()
    types = next(binding["event_types"] for binding in original["bindings"]
                 if binding["source_registry_id"] == sources[0].id)
    hass.states.async_set(renamed.entity_id, dt_util.utcnow().isoformat(),
        {"event_types": types, "event_type": types[0]})
    await hass.async_block_till_done()
    assert reload_entry.await_count == 0
    hass.states.async_set(renamed.entity_id, dt_util.utcnow().isoformat(),
        {"event_types": [*types, "new_custom_action"], "event_type": "new_custom_action"})
    await hass.async_block_till_done()
    assert reload_entry.await_count == 1
    assert next(iter(entry.subentries.values())).data[AUTOMATIC] == original
    assert hass.states.get(prepared["gesture_entity_id"]).attributes["bridge_update_kind"] == "snapshot"
