"""Actual HA acknowledgment service, read-only diagnostics and rename lifecycle."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import json
import logging

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
import voluptuous as vol

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge.diagnostics import async_get_config_entry_diagnostics
from custom_components.ha_gesture_bridge import latency
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent

CONFIG = {"platform": DOMAIN, "unique_id": "private-office-control", "name": "Private Office",
          "source_entity_id": "event.office_source", "actions": {"KeyPressed": {"kind": "press", "press_count": 1}}}


async def setup(hass):
    hass.states.async_set(CONFIG["source_entity_id"], "unknown")
    assert await async_setup_component(hass, "event", {"event": [deepcopy(CONFIG)]})
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, CONFIG["unique_id"])
    tracker = hass.data[DOMAIN]["latency"]
    entity = tracker.entities[CONFIG["unique_id"]]
    assert entity.entity_id == entity_id
    return entity, tracker


async def press(hass):
    hass.states.async_set(CONFIG["source_entity_id"], dt_util.utcnow().isoformat(), {"event_type": "KeyPressed"})
    await hass.async_block_till_done()


def payload(entity):
    return {"entity_id": entity.entity_id, "bridge_session_id": entity._session_id,
            "gesture_sequence": entity._sequence, "receiver": "control4:123",
            "receiver_received_epoch_ms": 1794729172042, "receiver_processed_epoch_ms": 1794729172048,
            "receiver_forwarded_epoch_ms": 1794729172047, "receiver_processing_ms": 6}


async def ack(hass, data):
    await hass.services.async_call(DOMAIN, "acknowledge", data, blocking=True)


async def test_ack_round_trip_ignores_clock_offsets_and_never_publishes_state(hass, monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr(latency, "monotonic", lambda: clock[0])
    entity, tracker = await setup(hass)
    assert hass.states.get(entity.entity_id).attributes["bridge_acknowledgment_version"] == 2
    assert "bridge_acknowledgment_supported" not in hass.states.get(entity.entity_id).attributes
    assert not tracker.pending and not tracker.samples  # A snapshot isn't a publication.
    await press(hass)
    before = hass.states.get(entity.entity_id)
    published = before.attributes["gesture_occurred_at"]
    clock[0] += .023
    # The receiver's clock is deliberately unrelated to HA's current UTC clock.
    data = payload(entity)
    with caplog.at_level(logging.INFO, logger="custom_components.ha_gesture_bridge.latency"):
        await ack(hass, data)
    assert hass.states.get(entity.entity_id) is before
    assert entity._sequence == 1
    sample = tracker.samples[0][1]
    assert sample["round_trip_ms"] == 23
    assert sample["ha_published_utc"] == published
    assert sample["receiver_forwarded_epoch_ms"] == data["receiver_forwarded_epoch_ms"]
    assert sample["receiver_processing_ms"] == 6
    assert "Gesture latency acknowledgment" in caplog.text
    with pytest.raises(ServiceValidationError, match="already acknowledged"):
        await ack(hass, data)
    assert len(tracker.samples) == 1
    entry = SimpleNamespace(subentries={"control": SimpleNamespace(subentry_type="control", data=CONFIG)})
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    latest = diagnostics["latency"]["latest_latency_samples"]
    assert latest == [{"control_index": 1, **sample}]
    serialized = json.dumps(diagnostics)
    for private in (CONFIG["unique_id"], CONFIG["name"], entity.entity_id, entity._session_id, data["receiver"]):
        assert private not in serialized
    assert hass.states.get(entity.entity_id) is before


async def test_ack_rejects_wrong_expired_removed_events_and_bounds_history(hass, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(latency, "monotonic", lambda: clock[0])
    entity, tracker = await setup(hass)
    await press(hass)
    data = payload(entity)
    for change in ({"bridge_session_id": "wrong-session"}, {"gesture_sequence": 999}, {"entity_id": "event.other"}):
        with pytest.raises(ServiceValidationError):
            await ack(hass, data | change)
    assert not tracker.samples
    clock[0] += 31
    with pytest.raises(ServiceValidationError, match="stale or unknown"):
        await ack(hass, data)
    assert not tracker.pending
    for _ in range(latency.MAX_PENDING + 3):
        await press(hass)
        data = payload(entity)
        # Programming-only gestures have no controller-forward timestamp.
        data.pop("receiver_forwarded_epoch_ms")
        await ack(hass, data)
    assert len(tracker.pending) == latency.MAX_PENDING
    assert len(tracker.samples) == latency.MAX_SAMPLES
    assert all("receiver_forwarded_epoch_ms" not in sample for _, sample in tracker.samples)
    data = payload(entity)
    await entity.async_remove()
    with pytest.raises(ServiceValidationError, match="not currently loaded"):
        await ack(hass, data)
    assert not tracker.pending and not tracker.entities


async def test_ack_preserves_wall_clock_steps_but_uses_monotonic_round_trip(hass, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(latency, "monotonic", lambda: clock[0])
    entity, tracker = await setup(hass)
    await press(hass)
    data = payload(entity)
    # A receiver NTP step must not destroy the reliable HA-clock RTT measurement.
    data["receiver_processed_epoch_ms"] -= 1000
    clock[0] += .042
    await ack(hass, data)
    sample = tracker.samples[0][1]
    assert sample["round_trip_ms"] == 42
    assert sample["receiver_clock_discontinuity"] is True
    assert sample["receiver_processed_epoch_ms"] == data["receiver_processed_epoch_ms"]


@pytest.mark.parametrize("change", [{"gesture_sequence": True}, {"receiver_processing_ms": -1},
    {"receiver_received_epoch_ms": float("nan")}, {"receiver_processed_epoch_ms": float("inf")},
    {"receiver_received_epoch_ms": 10**400},
    {"receiver_forwarded_epoch_ms": True}, {"receiver": "private payload with whitespace"}])
async def test_service_schema_rejects_invalid_diagnostic_values(hass, change):
    entity, tracker = await setup(hass)
    await press(hass)
    before = hass.states.get(entity.entity_id)
    with pytest.raises(vol.Invalid):
        await ack(hass, payload(entity) | change)
    assert not tracker.samples
    assert hass.states.get(entity.entity_id) is before


async def test_aggregate_registry_rename_readds_only_live_adapters(hass):
    entity, tracker = await setup(hass)
    await press(hass)
    original = entity.entity_id
    old_adapters = list(entity._adapters)
    registry = er.async_get(hass)
    registry.async_update_entity(original, new_entity_id="event.renamed_office_gestures")
    await hass.async_block_till_done()
    assert entity.entity_id == "event.renamed_office_gestures"
    assert len(entity._adapters) == len(old_adapters) == 1
    assert entity._adapters[0] is not old_adapters[0]
    assert old_adapters[0]._closed
    state = hass.states.get(entity.entity_id)
    assert len(state.attributes["bridge_sources"]) == 1
    assert len(tracker.entities) == 1
    # Rename itself is a baseline, then one fresh press produces one occurrence.
    sequence = entity._sequence
    await press(hass)
    assert entity._sequence == sequence + 1
    assert hass.states.get(entity.entity_id).attributes["gesture_sequence"] == sequence + 1
    assert hass.states.get(original) is None
