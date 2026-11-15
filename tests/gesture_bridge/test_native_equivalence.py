"""All saved native declaration forms share one live occurrence policy."""

from copy import deepcopy
from datetime import timedelta
from itertools import count
import logging

from homeassistant.components.event import EventEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent
from tools.validate_gesture_contract import validate_state


ACTIONS = {
    "double": {"kind": "press", "press_count": 2},
    "held": {"kind": "hold_start", "id": "ramp"},
    "repeat": {"kind": "hold_repeat", "id": "ramping"},
    "released": {"kind": "release", "id": "up"},
}


class Paddle(EventEntity):
    _attr_name = "Equivalence Source"
    _attr_event_types = list(ACTIONS)

    async def press(self, action):
        self._trigger_event(action)
        self.async_write_ha_state()
        await self.hass.async_block_till_done()


def declaration(form, source):
    config = {"unique_id": f"equivalence-{form}", "name": f"Equivalence {form}"}
    if form == "legacy":
        config.update(source_entity_id=source, actions=deepcopy(ACTIONS))
    elif form == "buttons":
        config["buttons"] = [{"id": "top", "label": "Top", "source_entity_id": source,
                              "actions": deepcopy(ACTIONS)}]
    elif form == "native":
        config["sources"] = [{"id": "paddle", "type": "native_event", "source_entity_id": source,
                              "actions": deepcopy(ACTIONS)}]
    else:
        config["sources"] = [{"id": "paddle", "type": "shared_native", "source_entity_id": source,
                              "buttons": [{"id": "top", "label": "Top"}],
                              "mappings": [{"button_id": "top", "match": {"event_type": action},
                                            "gesture": deepcopy(gesture)} for action, gesture in ACTIONS.items()]}]
    return NativeControlConfig.parse(config)


def attributes(hass, entity):
    state = hass.states.get(entity.entity_id).as_dict()
    validate_state(state)
    return state["attributes"]


def equivalent(hass, entities):
    rows = [attributes(hass, entity) for entity in entities]
    keys = ("bridge_update_kind", "bridge_buttons", "bridge_unmapped_count", "bridge_error_count",
            "event_type", "button_id", "gesture_id", "press_count", "gesture_sequence")
    baseline = {key: rows[0].get(key) for key in keys}
    for entity, row in zip(entities, rows, strict=True):
        assert {key: row.get(key) for key in keys} == baseline
        assert [(source["ready"], source["availability"]) for source in row["bridge_sources"]] == [
            (source["ready"], source["availability"]) for source in rows[0]["bridge_sources"]]
        assert entity._held_buttons == entities[0]._held_buttons
    # Saved legacy metadata shape remains unchanged after runtime unification.
    for row in rows[:2]:
        assert row["bridge_sources"][0]["id"] == "native-source"
        assert "button_id" not in row["bridge_sources"][0]
    for row in rows[2:]:
        assert row["bridge_sources"][0]["id"] == "paddle"
        assert row["bridge_sources"][0]["button_id"] == "top"
    return rows[0]


async def test_native_forms_have_identical_baseline_gesture_hold_and_recovery_policy(hass, monkeypatch):
    now, ticks = dt_util.utcnow(), count()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(milliseconds=next(ticks)))
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    source = Paddle()
    await component.async_add_entities([source])
    # A pre-existing occurrence is a baseline for every declaration form.
    hass.states.async_set(source.entity_id, (dt_util.utcnow() - timedelta(minutes=1)).isoformat(),
                          {"event_type": "double"})
    configs = [declaration(form, source.entity_id) for form in ("legacy", "buttons", "native", "shared")]
    entities = [GestureBridgeEvent(config) for config in configs]
    await component.async_add_entities(entities)
    await hass.async_block_till_done()
    assert "gesture_sequence" not in equivalent(hass, entities)
    assert equivalent(hass, entities)["bridge_update_kind"] == "snapshot"
    assert all(entity._sequence == 0 and not entity._held_buttons for entity in entities)

    for sequence, action, gesture in ((1, "double", "press_2"), (2, "double", "press_2"),
                                      (3, "held", "ramp"), (4, "held", "ramping"), (5, "released", "up")):
        await source.press(action)
        row = equivalent(hass, entities)
        assert row["gesture_sequence"] == sequence and row["gesture_id"] == gesture
    state = hass.states.get(source.entity_id)
    hass.states.async_set(source.entity_id, state.state, {**state.attributes, "battery": 42})
    await hass.async_block_till_done()
    assert equivalent(hass, entities)["gesture_sequence"] == 5
    assert equivalent(hass, entities)["bridge_update_kind"] == "snapshot"

    hass.states.async_set(source.entity_id, "unknown")
    await hass.async_block_till_done()
    row = equivalent(hass, entities)
    assert row["bridge_sources"][0]["ready"] is False
    assert row["bridge_sources"][0]["availability"] == "unknown"
    await source.press("held")
    assert equivalent(hass, entities)["gesture_id"] == "ramp"
    hass.states.async_set(source.entity_id, "unavailable")
    await hass.async_block_till_done()
    assert not entities[0]._held_buttons
    assert equivalent(hass, entities)["gesture_sequence"] == 6
    await source.press("held")  # Availability recovery establishes a baseline.
    assert equivalent(hass, entities)["gesture_sequence"] == 6
    await source.press("held")
    assert equivalent(hass, entities)["gesture_id"] == "ramp"
    assert equivalent(hass, entities)["gesture_sequence"] == 7

    # Removing and re-adding any form never turns its retained state into input.
    sessions = [attributes(hass, entity)["bridge_session_id"] for entity in entities]
    for entity in entities:
        await entity.async_remove()
        assert not entity._held_buttons
    entities = [GestureBridgeEvent(config) for config in configs]
    await component.async_add_entities(entities)
    await hass.async_block_till_done()
    # HA restores its last event metadata, which remains a snapshot carrying the
    # old gesture session. It does not increment the fresh occurrence sequence.
    assert equivalent(hass, entities)["bridge_update_kind"] == "snapshot"
    assert all(entity._sequence == 0 and not entity._held_buttons for entity in entities)
    assert all(attributes(hass, entity)["gesture_session_id"] == session
               for entity, session in zip(entities, sessions, strict=True))
    assert all(attributes(hass, entity)["bridge_session_id"] != session
               for entity, session in zip(entities, sessions, strict=True))
    await source.press("held")
    assert equivalent(hass, entities)["gesture_sequence"] == 1
    assert equivalent(hass, entities)["gesture_id"] == "ramp"
