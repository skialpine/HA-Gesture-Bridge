"""Real HA read-only revision/paging service; no synthetic transport shortcut."""
from unittest.mock import AsyncMock, patch
from types import MappingProxyType, SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntry, SOURCE_USER
from homeassistant.core import Context
from homeassistant.const import EntityCategory
from homeassistant.exceptions import Unauthorized, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge.device_catalog import selected_endpoint
from test_config_flow import install, device_fixture
from test_preparation import relay


async def read(hass, **data):
    return await hass.services.async_call(DOMAIN, "get_devices", data, blocking=True, return_response=True)


async def test_one_list_revision_is_stable_across_load_changes(hass, monkeypatch):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    load = relay(hass, device)
    forbidden = AsyncMock(side_effect=AssertionError("Browsing enumerated a provider"))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", forbidden)
    first = await read(hass)
    assert first["total"] == len(first["entities"]) == 1
    choice = first["entities"][0]["attributes"]["device_choice"]
    assert choice["device_id"] == device.id
    hass.states.async_set(load.entity_id, "on")
    assert await read(hass, revision=first["revision"]) == {
        "version": 1, "eligibility_version": 1, "revision": first["revision"], "unchanged": True,
        "page": 1, "pages": 1, "total": 1}
    er.async_get(hass).async_update_entity(load.entity_id, entity_category=EntityCategory.CONFIG)
    changed = await read(hass, revision=first["revision"])
    assert not changed["unchanged"] and changed["revision"] != first["revision"]
    assert not entry.subentries and forbidden.await_count == 0


async def test_native_groups_keep_kind_but_not_live_levels_or_c4_mirrors(hass):
    await install(hass)
    registry = er.async_get(hass)
    group = registry.async_get_or_create("light", "group", "native", original_name="Native Group")
    exported = registry.async_get_or_create("light", "control4", "export", original_name="C4 Group")
    for item in (group, exported):
        hass.states.async_set(item.entity_id, "on", {"entity_id": ["light.member"],
            "supported_color_modes": ["brightness"], "supported_features": 32, "brightness": 200})
    first = await read(hass)
    assert [row["entity_id"] for row in first["entities"]] == [group.entity_id]
    row = first["entities"][0]
    assert row["is_group"] and row["state"] == "unknown" and "brightness" not in row["attributes"]
    hass.states.async_set(group.entity_id, "off", {"entity_id": ["light.member"],
        "supported_color_modes": ["brightness"], "supported_features": 32, "brightness": None})
    assert (await read(hass, revision=first["revision"]))["unchanged"]


async def test_paging_is_bounded_and_rejects_a_changed_revision(hass):
    await install(hass)
    registry = er.async_get(hass)
    for index in range(200):
        item = registry.async_get_or_create("light", "group", str(index), original_name="Native Group " + str(index))
        hass.states.async_set(item.entity_id, "off", {"entity_id": ["light.member"], "supported_color_modes": ["onoff"]})
    first = await read(hass)
    assert first["eligibility_version"] == 1
    assert first["pages"] > 1 and len(first["entities"]) < 200
    rows = list(first["entities"])
    for page in range(2, first["pages"] + 1):
        result = await read(hass, revision=first["revision"], page=page)
        assert result["eligibility_version"] == 1
        rows.extend(result["entities"])
    assert len(rows) == len({row["entity_id"] for row in rows}) == 200
    registry.async_update_entity(item.entity_id, name="Changed")
    with pytest.raises(ServiceValidationError, match="snapshot_changed"):
        await read(hass, revision=first["revision"], page=2)


async def test_admin_gate_precedes_list_build(hass, monkeypatch):
    await install(hass)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.picker.picker_rows",
        lambda hass: (_ for _ in ()).throw(AssertionError("Read list before authorization")))
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "get_devices", {}, blocking=True, return_response=True,
            context=Context(user_id="ordinary-user"))


async def config_entry(hass, domain, **options):
    entry = ConfigEntry(
        domain=domain, title=domain, data={}, options=options, version=1, minor_version=1,
        source=SOURCE_USER, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[],
    )
    with patch.object(type(hass.config_entries), "async_setup", new_callable=AsyncMock):
        await hass.config_entries.async_add(entry)
    return entry


async def test_picker_visibility_comes_from_membership_and_entity_registry(hass):
    entry = await install(hass)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("fixture", "lamp")})
    load = relay(hass, device)
    group = registry.async_get_or_create("light", "group", "native", device_id=device.id)
    first = await read(hass)
    assert first["total"] == 2

    registry.async_update_entity(load.entity_id, hidden_by=er.RegistryEntryHider.USER)
    hidden = await read(hass, revision=first["revision"])
    assert not hidden["unchanged"] and [row["entity_id"] for row in hidden["entities"]] == [group.entity_id]

    devices.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    disabled = await read(hass, revision=hidden["revision"])
    assert not disabled["unchanged"] and disabled["entities"] == []


async def test_real_switch_as_light_config_hides_base_without_losing_visible_wrapper(hass):
    entry = await install(hass)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                                                   identifiers={("fixture", "lamp")})
    source = relay(hass, device)
    wrapper_entry = await config_entry(hass, "switch_as_x", entity_id=source.id, invert=False)
    wrapper = registry.async_get_or_create("light", "switch_as_x", "wrapper", config_entry=wrapper_entry)
    first = await read(hass)
    assert first["total"] == 1
    assert first["entities"][0]["attributes"]["device_choice"]["source_load"]["entity_id"] == wrapper.entity_id

    assert selected_endpoint(hass, device.id, source.id).id == wrapper.id
    registry.async_update_entity(source.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
    hidden = await read(hass, revision=first["revision"])
    assert hidden["unchanged"]
    with pytest.raises(ValueError, match="invalid_endpoint"):
        selected_endpoint(hass, device.id, source.id)

    second_entry = await config_entry(hass, "switch_as_x", entity_id=source.id, invert=False)
    alternate = registry.async_get_or_create("light", "switch_as_x", "wrapper_backup", config_entry=second_entry)
    merged = await read(hass, revision=first["revision"])
    assert merged["unchanged"]
    assert selected_endpoint(hass, device.id, alternate.id).id == wrapper.id

    registry.async_update_entity(wrapper.entity_id, hidden_by=er.RegistryEntryHider.USER)
    registry.async_update_entity(alternate.entity_id, hidden_by=er.RegistryEntryHider.USER)
    changed = await read(hass, revision=hidden["revision"])
    assert not changed["unchanged"] and changed["entities"] == []


async def test_control4_config_ownership_excludes_physical_endpoints_groups_and_wrappers(hass):
    entry = await install(hass)
    c4 = await config_entry(hass, "control4")
    registry, devices = er.async_get(hass), dr.async_get(hass)
    c4_device = devices.async_get_or_create(config_entry_id=c4.entry_id, identifiers={("fixture", "c4")})
    relay(hass, c4_device, "device_owned")
    registry.async_get_or_create("light", "group", "device_owned", device_id=c4_device.id)
    registry.async_get_or_create("light", "group", "entity_owned", config_entry=c4)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("fixture", "native")})
    exported = registry.async_get_or_create("switch", "fixture", "exported", device_id=device.id, config_entry=c4)
    wrapper_entry = await config_entry(hass, "switch_as_x", entity_id=exported.id, invert=True)
    registry.async_get_or_create("light", "switch_as_x", "wrapper", device_id=device.id, config_entry=wrapper_entry)
    assert (await read(hass))["entities"] == []
