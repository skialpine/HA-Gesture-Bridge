"""Export actual HA first-use action results and publications for C4 packages."""

import argparse
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from homeassistant import config_entries, loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.ha_gesture_bridge.config import DOMAIN


async def activation_fixture(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    saved = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    entry = saved["result"]
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture", "activation-device")}, name="Activation Switch")
    registry = er.async_get(hass)
    relay = registry.async_get_or_create("switch", "fixture", "activation-relay", device_id=device.id,
        suggested_object_id="activation_relay", original_name="Activation Relay")
    source = registry.async_get_or_create("event", "fixture", "activation-input", device_id=device.id,
        suggested_object_id="activation_input", original_name="Activation Button")
    bulb = registry.async_get_or_create("light", "fixture", "activation-bulb",
        suggested_object_id="activation_bulb", original_name="Linked Bulb")
    hass.states.async_set(relay.entity_id, "off")
    hass.states.async_set(bulb.entity_id, "off", {"supported_color_modes": ["brightness"], "supported_features": 32, "brightness": None})
    hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(), {"event_types": ["single", "double"]})
    await hass.async_block_till_done()
    ids = [relay.entity_id, source.entity_id, "sensor.ha_gesture_bridge_catalog"]

    def states():
        return [hass.states.get(identity).as_dict() for identity in ids]

    before = deepcopy(states())
    request = {"device_id": device.id, "source_registry_id": relay.id}
    first = await hass.services.async_call(DOMAIN, "prepare_device", request, blocking=True, return_response=True)
    await hass.async_block_till_done()
    ids.append(first["gesture_entity_id"])
    after = deepcopy(states())
    sub = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(entry, sub, data=dict(sub.data) | {"load_entity_id": bulb.entity_id})
    await hass.async_block_till_done()
    linked = await hass.services.async_call(DOMAIN, "prepare_device", request, blocking=True, return_response=True)
    await hass.async_block_till_done()
    ids.append(bulb.entity_id)
    linked_states = deepcopy(states())
    hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(),
        {"event_types": ["single", "double"], "event_type": "double"})
    await hass.async_block_till_done()
    gesture = hass.states.get(first["gesture_entity_id"]).as_dict()
    # Real lifecycle publications for packaged recovery tests. Missing native
    # metadata leaves the original recipe intact rather than discovering again.
    hass.states.async_remove(source.entity_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    waiting = await hass.services.async_call(DOMAIN, "prepare_device", request, blocking=True, return_response=True)
    waiting_states = deepcopy([hass.states.get(identity).as_dict() for identity in ids if hass.states.get(identity)])
    source = registry.async_update_entity(source.entity_id, new_entity_id="event.activation_input_renamed")
    ids[1] = source.entity_id
    hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(), {"event_types": ["single", "double"]})
    await hass.async_block_till_done()
    renamed = await hass.services.async_call(DOMAIN, "prepare_device", request, blocking=True, return_response=True)
    renamed_states = deepcopy(states())
    # Clearing the explicit link returns authority to each consumer's own
    # endpoint; a second endpoint must not overwrite the shared control.
    sub = next(iter(entry.subentries.values()))
    data = dict(sub.data)
    data.pop("load_entity_id", None)
    data.pop("load_registry_id", None)
    hass.config_entries.async_update_subentry(entry, sub, data=data)
    other = registry.async_get_or_create("switch", "fixture", "activation-other-relay", device_id=device.id,
        suggested_object_id="activation_other_relay", original_name="Other Relay")
    hass.states.async_set(other.entity_id, "off")
    await hass.async_block_till_done()
    endpoints = await asyncio.gather(*[hass.services.async_call(DOMAIN, "prepare_device",
        {"device_id": device.id, "source_registry_id": item.id}, blocking=True, return_response=True)
        for item in (relay, other)])
    ids.append(other.entity_id)
    endpoint_states = deepcopy(states())
    sub = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(entry, sub,
        data=dict(sub.data) | {"load_entity_id": bulb.entity_id, "load_registry_id": bulb.id})
    registry.async_remove(bulb.entity_id)
    replacement = registry.async_get_or_create("light", "fixture", "replacement-bulb",
        suggested_object_id="activation_bulb", original_name="Replacement Bulb")
    hass.states.async_set(replacement.entity_id, "on", {"supported_color_modes": ["brightness"], "brightness": 255})
    await hass.async_block_till_done()
    broken = await hass.services.async_call(DOMAIN, "prepare_device", request, blocking=True, return_response=True)
    broken_states = deepcopy(states())
    replacements = {device.id: "a" * 32, relay.id: "b" * 32, source.id: "c" * 32,
                    bulb.id: "d" * 32, other.id: "e" * 32, first["control_id"]: "activation-control"}
    sessions = dict.fromkeys(state["attributes"].get("bridge_session_id") for state in
                [*after, *linked_states, gesture, *waiting_states, *renamed_states, *endpoint_states, *broken_states]
                if state["attributes"].get("bridge_session_id"))
    for index, session in enumerate(sessions):
        replacements[session] = f"activation-session-{index}"

    def normalize(value):
        raw = json.dumps(value, default=str)
        for old, new in replacements.items():
            raw = raw.replace(old, new)
        return json.loads(raw)

    def portable(values):
        values = normalize(values)
        for state in values:
            for field in ("last_changed", "last_updated", "last_reported"):
                state[field] = "2026-11-16T12:00:00.000Z"
            state.pop("context", None)
            attrs = state["attributes"]
            if state["entity_id"].startswith("event.") and dt_util.parse_datetime(state["state"]):
                state["state"] = "2026-11-16T12:00:01.000+00:00"
            if attrs.get("gesture_occurred_at"):
                attrs["gesture_occurred_at"] = "2026-11-16T12:00:01.000+00:00"
                state["state"] = attrs["gesture_occurred_at"]
            for prefix in ("catalog_names", "catalog_devices"):
                if prefix + "_pages" in attrs:
                    if prefix == "catalog_devices":
                        for index in range(1, attrs[prefix + "_pages"] + 1):
                            attrs[f"{prefix}_page_{index:03d}"].sort(key=lambda row: row["choice_id"])
                    rows = [row for index in range(1, attrs[prefix + "_pages"] + 1)
                            for row in attrs[f"{prefix}_page_{index:03d}"]]
                    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                    attrs[prefix + "_revision"] = sha256(canonical.encode()).hexdigest()
            if "catalog_entities" in attrs:
                raw = json.dumps(attrs["catalog_entities"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                attrs["catalog_revision"] = sha256(raw.encode()).hexdigest()
        return values

    gesture = portable([gesture])[0]
    gesture["state"] = gesture["attributes"]["gesture_occurred_at"] = "2026-11-16T12:00:01.000+00:00"
    gesture["last_updated"] = "2026-11-16T12:00:01.000Z"
    return {"fixture_version": 1, "producer": DOMAIN, "catalog_entity_id": "sensor.ha_gesture_bridge_catalog",
            "before_states": portable(before), "after_states": portable(after), "linked_states": portable(linked_states),
            "preparation_result": normalize(first), "linked_result": normalize(linked), "fresh_gesture": gesture,
            "waiting_result": normalize(waiting), "waiting_states": portable(waiting_states),
            "renamed_result": normalize(renamed), "renamed_states": portable(renamed_states),
            "endpoint_results": normalize(endpoints), "endpoint_states": portable(endpoint_states),
            "broken_result": normalize(broken), "broken_states": portable(broken_states)}


async def export():
    with tempfile.TemporaryDirectory(prefix="ha-activation-fixture-") as directory:
        root = Path(__file__).resolve().parents[1]
        shutil.copytree(root / "custom_components/ha_gesture_bridge", Path(directory) / "custom_components/ha_gesture_bridge",
                        ignore=shutil.ignore_patterns("__pycache__"))
        hass = HomeAssistant(directory)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        loader.async_setup(hass)
        assert await async_load_base_functionality(hass)
        try:
            return await activation_fixture(hass)
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(asyncio.run(export()), sort_keys=True, indent=2) + "\n")
