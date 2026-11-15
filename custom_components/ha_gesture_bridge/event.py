"""Aggregate HA-owned gesture sources and independent linked-load snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.components.event import EventEntity, PLATFORM_SCHEMA as EVENT_PLATFORM_SCHEMA
from homeassistant.core import Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .config import CONTRACT_VERSION, DOMAIN, NativeButtonConfig, NativeControlConfig
from .sources import SourceAdapter
from .sources.native import NativeSourceBinding

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = EVENT_PLATFORM_SCHEMA.extend({
    vol.Required("unique_id"): cv.string,
    vol.Required("name"): cv.string,
    vol.Optional("source_entity_id"): cv.entity_id,
    vol.Optional("source_registry_id"): cv.string,
    vol.Optional("button_id"): cv.string,
    vol.Optional("button_label"): cv.string,
    vol.Optional("actions"): dict,
    vol.Optional("buttons"): list,
    vol.Optional("sources"): list,
    vol.Optional("load_entity_id"): cv.entity_id,
})


async def async_setup_platform(hass, config: dict[str, Any], async_add_entities: AddEntitiesCallback,
                               discovery_info: dict[str, Any] | None = None) -> None:
    """Keep the legacy shorthand and explicit multi-button development setup."""
    try:
        control = NativeControlConfig.parse(config)
    except ValueError as error:
        raise vol.Invalid(str(error)) from error
    entity = GestureBridgeEvent(control)
    hass.data.setdefault(DOMAIN, {}).setdefault("yaml_controls", {})[control.control_id] = {"config": dict(config), "entity": entity}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if control.control_id in entry.data.get("imported_yaml", []):
            return
        if any(sub.unique_id == control.control_id or sub.data.get("unique_id") == control.control_id
               for sub in entry.get_subentries_of_type("control")):
            return
    async_add_entities([entity])


async def async_setup_entry(hass, entry, async_add_entities):
    """Create independently managed UI controls with stable subentry ownership."""
    for subentry in entry.get_subentries_of_type("control"):
        config = NativeControlConfig.parse(dict(subentry.data))
        async_add_entities([GestureBridgeEvent(config)], config_subentry_id=subentry.subentry_id)


def source_timestamp(state: State | None) -> datetime | None:
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    timestamp = dt_util.parse_datetime(state.state)
    if timestamp is None or timestamp.tzinfo is None:
        return None
    return dt_util.as_utc(timestamp)


@dataclass
class _Source:
    button: NativeButtonConfig
    ready: bool = False
    availability: str = "unknown"
    error: str | None = None
    watermark: datetime | None = None
    held: bool = False


class GestureBridgeEvent(EventEntity):
    """Keep independent source history and shared monotonic occurrence identity."""

    _attr_should_poll = False

    def __init__(self, config: NativeControlConfig) -> None:
        self.config = config
        self._attr_unique_id = config.control_id
        self._attr_name = config.name
        self._sources = {} if config.source_configs else {button.source_entity_id: _Source(button) for button in config.inputs}
        self._adapters = []
        self._native_bindings = []
        self._held_buttons = set()
        self._unmapped_count = 0
        self._error_count = 0
        self._attr_event_types = list(dict.fromkeys(gesture["kind"] for button in config.buttons()
                                                   for gesture in button["gestures"]))
        self._session_id = uuid4().hex
        self._sequence = 0
        self._update_kind = "snapshot"
        started = dt_util.utcnow()
        # Native EventEntity states use milliseconds; a genuinely fresh first
        # occurrence in this same millisecond must not compare older than init.
        self._started_at = started.replace(microsecond=started.microsecond // 1000 * 1000)

    @property
    def _source_ready(self) -> bool:
        """Compatibility for single-source integrations and existing checks."""
        return next(iter(self._sources.values())).ready if self._sources else bool(self._adapters and self._adapters[0].ready)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Stay HA-available while loaded so readiness/load metadata remains visible.
        sources = []
        multiple = len(self._sources) > 1
        for source in self._sources.values():
            item = {"id": f"native-{source.button.id}" if multiple else "native-source",
                    "ready": source.ready, "availability": source.availability}
            if multiple:
                item["button_id"] = source.button.id
            if source.error:
                item["error"] = source.error
            sources.append(item)
        for adapter in self._adapters:
            for button in adapter.config.buttons:
                item = {"id": adapter.config.id if len(adapter.config.buttons) == 1 else f"{adapter.config.id}-{button.id}",
                        "button_id": button.id, "ready": adapter.ready, "availability": adapter.availability}
                if adapter.error:
                    item["error"] = adapter.error
                sources.append(item)
        version = "1.1" if self.config.load_entity_id and self.config.load_entity_id.startswith("light.") else CONTRACT_VERSION
        return {"bridge_contract_version": version, "bridge_control_id": self.config.control_id,
                "bridge_session_id": self._session_id, "bridge_update_kind": self._update_kind,
                "bridge_buttons": self.config.buttons(), "bridge_sources": sources,
                "bridge_load": self._load_snapshot(), "bridge_unmapped_count": self._unmapped_count,
                "bridge_error_count": self._error_count}

    def _load_snapshot(self) -> dict[str, Any] | None:
        if (target := self.config.load_entity_id) is None:
            return None
        state = self.hass.states.get(target)
        value = state.state if state is not None and state.state in ("on", "off", "unknown", "unavailable") else "unavailable"
        domain = target.split(".", 1)[0]
        result = {"entity_id": target, "domain": domain, "state": value,
                  "available": state is not None and state.state in ("on", "off")}
        if domain == "light":
            projected = {}
            if state is not None:
                attrs = state.attributes
                modes = attrs.get("supported_color_modes")
                known = {"onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
                if (isinstance(modes, (list, tuple)) and len(modes) <= 16
                        and all(isinstance(mode, str) and mode in known for mode in modes)
                        and len(set(modes)) == len(modes)):
                    projected["supported_color_modes"] = list(modes)
                features = attrs.get("supported_features")
                if type(features) is int and 0 <= features <= 4294967295:
                    projected["supported_features"] = features
                brightness = attrs.get("brightness")
                if (type(brightness) in (int, float) and 0 <= brightness <= 255
                        and math.isfinite(brightness)):
                    projected["brightness"] = brightness
            result["attributes"] = projected
        return result

    def _set_source_status(self, source: _Source, state: State | None) -> None:
        if state is not None and "bridge_contract_version" in state.attributes:
            error = "A bridge aggregate cannot be used as a native input source"
            if source.error != error:
                _LOGGER.error("%s/%s: %s", self.config.control_id, source.button.id, error)
            source.error, source.ready, source.availability, source.held = error, False, "unavailable", False
            return
        source.error = None
        source.ready = state is not None and state.state != "unavailable" and not state.attributes.get("restored", False)
        source.availability = "available" if source.ready else "unavailable"
        if not source.ready:
            source.held = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_id in self._sources:
            raise ValueError("A control cannot consume its own aggregate entity")
        for source_config in self.config.source_configs:
            if source_config.source_entity_id == self.entity_id:
                raise ValueError("A control cannot consume its own aggregate entity")
            adapter = SourceAdapter(self.hass, source_config, self._adapter_gesture, self._adapter_status,
                                    self._adapter_idle, self._adapter_unmapped)
            self._adapters.append(adapter)
            self.async_on_remove(adapter.close)
            await adapter.async_start()
            native = getattr(adapter, "native_binding", None)
            if native and native.entity_id and any(
                getattr(other, "native_binding", None) and other.native_binding.entity_id == native.entity_id
                for other in self._adapters[:-1]
            ):
                adapter.close()
                adapter.ready, adapter.availability, adapter.error = False, "unavailable", "duplicate_native_source"
            if adapter.error:
                self._error_count += 1
        for entity_id, source in list(self._sources.items()):
            def baseline(target, state, initial, old_entity_id, source=source, declared=entity_id):
                self._sources.pop(old_entity_id or declared, None)
                if target and target in self._sources and self._sources[target] is not source:
                    source.ready, source.availability, source.error, source.held = False, "unavailable", "duplicate_native_source", False
                    self._sources[declared] = source
                    if not initial:
                        self._snapshot()
                    return False
                # An absent registry identity remains a tracked, unavailable
                # source but never binds a replacement with the old entity ID.
                self._sources[target or declared] = source
                source.held = False
                self._set_source_status(source, state)
                source.watermark = source_timestamp(state)
                if not initial:
                    self._snapshot()
            binding = NativeSourceBinding(self.hass, entity_id, source.button.source_registry_id,
                                          self._source_changed, baseline)
            self._native_bindings.append(binding)
            self.async_on_remove(binding.close)
            binding.start()
        if self.config.load_entity_id:
            self.async_on_remove(async_track_state_change_event(self.hass, [self.config.load_entity_id], self._load_changed))
        self.async_on_remove(self._clear_holds)

    @callback
    def _clear_holds(self) -> None:
        self._held_buttons.clear()
        for source in self._sources.values():
            source.held = False

    @callback
    def _adapter_idle(self, adapter):
        for button in adapter.config.buttons:
            self._held_buttons.discard(button.id)

    @callback
    def _adapter_status(self, adapter):
        if adapter.error:
            self._error_count += 1
        self._snapshot()

    @callback
    def _adapter_unmapped(self, adapter, ambiguous=False):
        self._unmapped_count += 1
        if ambiguous:
            self._error_count += 1
        self._snapshot()

    @callback
    def _adapter_gesture(self, adapter, button_id, gesture):
        button = next(button for button in adapter.config.buttons if button.id == button_id)
        if gesture.kind == "hold_start":
            if button_id in self._held_buttons:
                gesture = button.repeat_gesture()
            self._held_buttons.add(button_id)
        elif gesture.kind in {"release", "press"}:
            self._held_buttons.discard(button_id)
        self._publish_gesture(button_id, gesture)

    @callback
    def _publish_gesture(self, button_id, gesture):
        self._sequence += 1
        self._update_kind = "gesture"
        attributes = {"button_id": button_id, "gesture_id": gesture.id,
                      "gesture_session_id": self._session_id, "gesture_sequence": self._sequence,
                      "gesture_event_id": f"{self._session_id}:{self._sequence}",
                      "gesture_occurred_at": dt_util.utcnow().isoformat(timespec="milliseconds")}
        if gesture.press_count is not None:
            attributes["press_count"] = gesture.press_count
        self._trigger_event(gesture.kind, attributes)
        self.async_write_ha_state()

    @callback
    def _snapshot(self) -> None:
        self._update_kind = "snapshot"
        self.async_write_ha_state()

    @callback
    def _load_changed(self, event: Event) -> None:
        self._snapshot()

    @callback
    def _source_changed(self, event: Event) -> None:
        source = self._sources.get(event.data.get("entity_id"))
        if source is None:
            return
        state: State | None = event.data["new_state"]
        old_state: State | None = event.data["old_state"]
        self._set_source_status(source, state)
        timestamp = source_timestamp(state)
        if not source.ready or timestamp is None:
            if timestamp is None:
                source.held = False
                # A live source lost event tracking. Publish readiness loss so
                # C4 can stop that button's outbound hold without inventing a
                # physical release. Initial unknown source setup stays ready.
                source.ready = False
                if source.availability != "unavailable":
                    source.availability = "unknown"
            self._snapshot()
            return
        # Creation/recovery is a baseline, never a replayed physical gesture.
        if old_state is None or old_state.state == "unavailable":
            source.watermark = max(timestamp, source.watermark) if source.watermark else timestamp
            source.held = False
            self._snapshot()
            return
        if timestamp < self._started_at or (source.watermark is not None and timestamp <= source.watermark):
            self._snapshot()
            return
        source.watermark = timestamp
        action = state.attributes.get("event_type")
        gesture = source.button.actions.get(action) if isinstance(action, str) else None
        if gesture is None:
            self._unmapped_count += 1
            _LOGGER.warning("Unmapped action for gesture control %s/%s", self.config.control_id, source.button.id)
            self._snapshot()
            return
        if gesture.kind == "hold_start":
            if source.held:
                gesture = source.button.repeat_gesture()
            source.held = True
        elif gesture.kind in ("release", "press"):
            source.held = False
        self._publish_gesture(source.button.id, gesture)
