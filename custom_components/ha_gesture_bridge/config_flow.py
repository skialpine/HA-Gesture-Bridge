"""Installation and per-control setup without ongoing YAML configuration."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import enrich_native_sources
from .config import DOMAIN, NativeControlConfig

SUBENTRY_CONTROL = "control"
SOURCE_TYPES = ["native_event", "shared_native", "zwave_js", "zha", "mqtt", "bus", "advanced"]
PROFILE_ACTIONS = {
    "KeyPressed": {"kind": "press", "press_count": 1},
    **{f"KeyPressed{count}x": {"kind": "press", "press_count": count} for count in range(2, 6)},
    "KeyHeldDown": {"kind": "hold_start"}, "KeyReleased": {"kind": "release"},
    "single": {"kind": "press", "press_count": 1}, "double": {"kind": "press", "press_count": 2},
    "triple": {"kind": "press", "press_count": 3}, "single_press": {"kind": "press", "press_count": 1},
    "double_press": {"kind": "press", "press_count": 2}, "triple_press": {"kind": "press", "press_count": 3},
    "hold": {"kind": "hold_start"}, "release": {"kind": "release"},
}
LOAD_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain=["switch", "input_boolean", "light"]))
EVENT_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="event"))
TEXT = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


class MQTTNotConfigured(ValueError):
    """The optional MQTT source requires the user's MQTT integration."""


def yaml_controls(hass) -> dict[str, dict]:
    """Read controls already loaded by the backwards-compatible YAML platform."""
    return hass.data.get(DOMAIN, {}).get("yaml_controls", {})


def clean_config(data: dict) -> dict:
    """Remove only HA's platform metadata; keep stable identities and mappings."""
    return {key: deepcopy(value) for key, value in data.items() if key != "platform"}


class GestureBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a single installation entry with independently editable controls."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry):
        return {SUBENTRY_CONTROL: ControlSubentryFlow}

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            subentries = []
            if user_input.get("import_yaml"):
                for identity, record in yaml_controls(self.hass).items():
                    config = enrich_native_sources(self.hass, clean_config(record["config"]))
                    try:
                        parsed = NativeControlConfig.parse(config)
                    except (ValueError, TypeError):
                        continue
                    subentries.append({"data": config, "subentry_type": SUBENTRY_CONTROL,
                                       "title": parsed.name, "unique_id": identity})
            return self.async_create_entry(title="HA Gesture Bridge", data={
                "imported_yaml": [item["unique_id"] for item in subentries]}, subentries=subentries)
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Optional("import_yaml", default=bool(yaml_controls(self.hass))): bool}))


class ControlSubentryFlow(ConfigSubentryFlow):
    """Add and reconfigure a control; HA supplies removal in its subentry UI."""

    def __init__(self):
        self._identity = uuid4().hex
        self._name = ""
        self._source_type = "native_event"
        self._editing = None
        self._existing: dict[str, Any] = {}

    async def async_step_reconfigure(self, user_input=None):
        if self._editing is None:
            self._editing = self._get_reconfigure_subentry()
            self._existing = enrich_native_sources(self.hass, dict(self._editing.data))
            self._identity = self._editing.unique_id or self._existing["unique_id"]
            self._name = self._existing.get("name", self._editing.title)
            # Editing through JSON preserves arbitrary/custom button IDs. The
            # generated control identity is never an editable form field.
            self._source_type = "advanced"
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._name = user_input["name"].strip()
            self._source_type = user_input["source_type"]
            if self._source_type == "import_yaml":
                return await self.async_step_import_yaml()
            if self._source_type == "native_event":
                return await self.async_step_native()
            if self._source_type != "advanced":
                return await self.async_step_source()
            return await self.async_step_advanced()
        types = list(SOURCE_TYPES)
        if not self._editing and yaml_controls(self.hass):
            types.append("import_yaml")
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("name", default=self._name or "Gesture Control"): str,
            vol.Required("source_type", default=self._source_type): selector.SelectSelector(
                selector.SelectSelectorConfig(options=types, translation_key="source_type"))}))

    def _finish(self, data):
        data = enrich_native_sources(self.hass, clean_config(data))
        data["unique_id"] = self._identity
        data["name"] = self._name
        parsed = NativeControlConfig.parse(data)
        if any(source.type == "mqtt" for source in parsed.source_configs):
            if "mqtt" not in self.hass.config.components and not self.hass.config_entries.async_entries("mqtt"):
                raise MQTTNotConfigured
        entry = self._get_entry()
        if any(item.unique_id == self._identity and item != self._editing for item in entry.subentries.values()):
            return self.async_abort(reason="already_configured")
        if self._editing:
            return self.async_update_and_abort(entry, self._editing, data=data, title=self._name)
        return self.async_create_entry(title=self._name, data=data, unique_id=self._identity)

    def _native_actions(self, entity: str) -> dict:
        state = self.hass.states.get(entity)
        event_types = state.attributes.get("event_types", []) if state else []
        if not isinstance(event_types, (list, tuple)):
            return {}
        # No manufacturer inference or universal five-click declaration: only
        # capabilities advertised by this actual source are enabled.
        return {action: deepcopy(PROFILE_ACTIONS[action]) for action in event_types
                if isinstance(action, str) and action in PROFILE_ACTIONS}

    async def async_step_native(self, user_input=None):
        errors = {}
        if user_input is not None:
            buttons = []
            try:
                for button, field in (("top", "top_entity_id"), ("bottom", "bottom_entity_id")):
                    if entity := user_input.get(field):
                        actions = self._native_actions(entity)
                        if not actions:
                            errors[field] = "no_supported_events"
                            continue
                        buttons.append({"id": button, "label": button.title(), "source_entity_id": entity, "actions": actions})
                if not errors:
                    data = {"buttons": buttons}
                    if user_input.get("load_entity_id"):
                        data["load_entity_id"] = user_input["load_entity_id"]
                    return self._finish(data)
            except (ValueError, TypeError, KeyError):
                errors["base"] = "invalid_mapping"
        return self.async_show_form(step_id="native", data_schema=vol.Schema({
            vol.Required("top_entity_id"): EVENT_SELECTOR,
            vol.Optional("bottom_entity_id"): EVENT_SELECTOR,
            vol.Optional("load_entity_id"): LOAD_SELECTOR,
        }), errors=errors)

    async def async_step_advanced(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                data = json.loads(user_input["configuration"])
                if not isinstance(data, dict):
                    raise ValueError("Configuration must be an object")
                return self._finish(data)
            except MQTTNotConfigured:
                errors["base"] = "mqtt_not_configured"
            except (ValueError, TypeError, KeyError, AttributeError):
                errors["base"] = "invalid_mapping"
        default = self._existing or {"sources": [{"id": "source_1", "type": self._source_type,
            "buttons": [{"id": "top", "label": "Top"}], "mappings": []}]}
        return self.async_show_form(step_id="advanced", data_schema=vol.Schema({
            vol.Required("configuration", default=json.dumps(default, indent=2)): TEXT,
        }), errors=errors)

    async def async_step_source(self, user_input=None):
        """Guide source identity selection while keeping mappings explicit."""
        errors = {}
        if user_input is not None:
            try:
                source = {"id": "source_1", "type": self._source_type,
                          "buttons": json.loads(user_input["buttons"]),
                          "mappings": json.loads(user_input["mappings"])}
                for field in ("source_entity_id", "device_id", "topic", "availability_topic", "event_type"):
                    if user_input.get(field):
                        source[field] = user_input[field]
                if "filters" in user_input:
                    source["filters"] = json.loads(user_input["filters"])
                data = {"sources": [source]}
                if user_input.get("load_entity_id"):
                    data["load_entity_id"] = user_input["load_entity_id"]
                return self._finish(data)
            except MQTTNotConfigured:
                errors["base"] = "mqtt_not_configured"
            except (ValueError, TypeError, KeyError, AttributeError):
                errors["base"] = "invalid_mapping"
        fields = {}
        if self._source_type in ("zwave_js", "zha"):
            fields[vol.Required("device_id")] = selector.DeviceSelector(
                selector.DeviceSelectorConfig(integration=self._source_type))
        elif self._source_type == "shared_native":
            fields[vol.Required("source_entity_id")] = EVENT_SELECTOR
        elif self._source_type == "mqtt":
            fields[vol.Required("topic")] = str
            fields[vol.Optional("availability_topic")] = str
        elif self._source_type == "bus":
            fields[vol.Required("event_type")] = str
            fields[vol.Required("filters", default='{"device_id": "your-device-id"}')] = TEXT
        match = {"event_type": "single"} if self._source_type == "shared_native" else {"action": "single"}
        if self._source_type == "zwave_js":
            match = {"scene": 1, "value": "KeyPressed"}
        elif self._source_type == "zha":
            match = {"command": "single"}
        mapping = [{"button_id": "top", "match": match, "gesture": {"kind": "press", "press_count": 1}}]
        fields[vol.Required("buttons", default='[{"id": "top", "label": "Top"}]')] = TEXT
        fields[vol.Required("mappings", default=json.dumps(mapping, indent=2))] = TEXT
        fields[vol.Optional("load_entity_id")] = LOAD_SELECTOR
        return self.async_show_form(step_id="source", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_import_yaml(self, user_input=None):
        entry = self._get_entry()
        existing = {item.unique_id for item in entry.subentries.values()}
        candidates = {identity: record for identity, record in yaml_controls(self.hass).items() if identity not in existing}
        if not candidates:
            return self.async_abort(reason="no_yaml_controls")
        errors = {}
        selected = user_input.get("control") if user_input else None
        if user_input is not None:
            if selected not in candidates:
                errors["base"] = "invalid_mapping"
            else:
                try:
                    data = clean_config(candidates[selected]["config"])
                    self._identity = selected
                    self._name = data["name"]
                    result = self._finish(data)
                except MQTTNotConfigured:
                    errors["base"] = "mqtt_not_configured"
                except (ValueError, TypeError, KeyError, AttributeError):
                    errors["base"] = "invalid_mapping"
                else:
                    if result["type"] == "create_entry":
                        imported = list(entry.data.get("imported_yaml", []))
                        if selected not in imported:
                            imported.append(selected)
                            self.hass.config_entries.async_update_entry(entry, data=dict(entry.data) | {"imported_yaml": imported})
                    return result
        field = vol.Required("control", default=selected) if selected in candidates else vol.Required("control")
        return self.async_show_form(step_id="import_yaml", data_schema=vol.Schema({
            field: selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {"value": identity, "label": record["config"]["name"]} for identity, record in candidates.items()]))}),
            errors=errors)
