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
from .device_discovery import UnsupportedDeviceConfiguration, discover_device
from .led_discovery import led_summary

SUBENTRY_CONTROL = "control"
DEVICE_SELECTOR = selector.DeviceSelector(selector.DeviceSelectorConfig())
LOAD_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain=["switch", "input_boolean", "light"]))
TEXT = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


class MQTTNotConfigured(ValueError):
    """The optional MQTT source requires the user's MQTT integration."""


def yaml_controls(hass) -> dict[str, dict]:
    """Read controls already loaded by the backwards-compatible YAML platform."""
    return hass.data.get(DOMAIN, {}).get("yaml_controls", {})


def clean_config(data: dict) -> dict:
    """Keep control fields while leaving YAML-only platform metadata in YAML.

    Registry ownership preserves the existing namespaced entity ID on import;
    non-polling controls have no UI use for a timedelta scan interval.
    """
    return {key: deepcopy(value) for key, value in data.items()
            if key not in {"platform", "entity_namespace", "scan_interval"}}


def require_source_dependencies(hass, control: NativeControlConfig) -> None:
    """Use the same dependency validation for creation and every import path."""
    if (any(source.type == "mqtt" for source in control.source_configs)
            and "mqtt" not in hass.config.components and not hass.config_entries.async_entries("mqtt")):
        raise MQTTNotConfigured


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
        errors = {}
        if user_input is not None:
            subentries = []
            if user_input.get("import_yaml"):
                for identity, record in yaml_controls(self.hass).items():
                    config = enrich_native_sources(self.hass, clean_config(record["config"]))
                    try:
                        parsed = NativeControlConfig.parse(config)
                    except (ValueError, TypeError):
                        continue
                    try:
                        require_source_dependencies(self.hass, parsed)
                    except MQTTNotConfigured:
                        errors["base"] = "mqtt_not_configured"
                        break
                    subentries.append({"data": config, "subentry_type": SUBENTRY_CONTROL,
                                       "title": parsed.name, "unique_id": identity})
            if not errors:
                return self.async_create_entry(title="HA Gesture Bridge", data={
                    "imported_yaml": [item["unique_id"] for item in subentries]}, subentries=subentries)
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Optional("import_yaml", default=bool(yaml_controls(self.hass))): bool}), errors=errors, last_step=True)


class ControlSubentryFlow(ConfigSubentryFlow):
    """Choose a device, review its actual inputs, and create one control."""

    def __init__(self):
        self._identity = uuid4().hex
        self._editing = None
        self._existing: dict[str, Any] = {}
        self._discovery = None
        self._device_id = None
        self._name = ""
        self._configure_leds = False
        self._pending: dict[str, Any] = {}

    async def async_step_reconfigure(self, user_input=None):
        if self._editing is None:
            self._editing = self._get_reconfigure_subentry()
            self._existing = enrich_native_sources(self.hass, dict(self._editing.data))
            self._identity = self._editing.unique_id or self._existing["unique_id"]
            self._device_id = self._existing.get("source_device_id")
            self._name = self._existing.get("name", self._editing.title)
        return await self.async_step_user(user_input)

    def _already_managed(self):
        return any(item != self._editing and item.data.get("source_device_id") == self._device_id
                   for item in self._get_entry().subentries.values())

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._device_id = user_input["device_id"]
            self._configure_leds = user_input.get("configure_leds", False)
            try:
                if self._already_managed():
                    return self._device_form({"device_id": "device_already_configured"})
                self._discovery = await discover_device(self.hass, self._device_id)
                if not self._discovery.buttons:
                    errors["device_id"] = "no_supported_inputs"
                else:
                    if not self._editing:
                        self._name = self._discovery.device_name[:119] + " Gestures"
                    return self._review_form()
            except UnsupportedDeviceConfiguration as error:
                return self._device_form({"device_id": "unsupported_device_configuration"}, str(error))
            except ValueError:
                errors["device_id"] = "device_unavailable"
        return self._device_form(errors)

    def _device_form(self, errors, hint=""):
        device = vol.Required("device_id", default=self._device_id) if self._device_id else vol.Required("device_id")
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            device: DEVICE_SELECTOR,
            vol.Optional("configure_leds", default=self._configure_leds): bool}), errors=errors,
            description_placeholders={"hint": hint}, last_step=False)

    def _same_inputs(self, discovery):
        return discovery.buttons == self._discovery.buttons

    def _roles(self, user_input):
        roles = {button.button_id: button.button_id for button in self._discovery.buttons}
        assigned = set()
        for role in ("top", "bottom", "toggle"):
            chosen = user_input.get(role + "_button", "__none__")
            if chosen != "__none__":
                if chosen not in roles or chosen in assigned:
                    raise ValueError("Choose each button at most once")
                roles[chosen] = role
                assigned.add(chosen)
        return roles

    def _review_form(self, user_input=None, error=None, hint=""):
        user_input = user_input or {}
        existing_roles = self._existing.get("source_button_roles", {})
        schema = {vol.Required("name", default=user_input.get("name", self._name)): str}
        options = [{"value": "__none__", "label": "None — keep original button names"}, *[
            {"value": button.button_id, "label": button.label + " — " + button.gesture_summary}
            for button in self._discovery.buttons]]
        for role in ("top", "bottom", "toggle"):
            default = next((button.button_id for button in self._discovery.buttons
                if existing_roles.get(button.button_id, button.suggested_role) == role), "__none__")
            field = role + "_button"
            schema[vol.Required(field, default=user_input.get(field, default))] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN))
        load = user_input.get("load_entity_id", self._existing.get("load_entity_id"))
        schema[vol.Optional("load_entity_id", default=load) if load else vol.Optional("load_entity_id")] = LOAD_SELECTOR
        return self.async_show_form(step_id="review_leds" if self._configure_leds else "review",
            data_schema=vol.Schema(schema), errors={"base": error} if error else {},
            description_placeholders={"device": self._discovery.device_name,
                "suggested_name": self._discovery.device_name[:119] + " Gestures", "hint": hint,
                "led_notice": ("Previous device LED mappings will be removed; supported LEDs on the new device will be discovered automatically."
                    if self._existing.get("leds") and self._existing.get("source_device_id") != self._device_id else ""),
                "led_summary": ("Custom LED mappings are active for this device. Use Customize LED mappings to edit them; save [] to return to automatic discovery."
                    if self._existing.get("leds") and self._existing.get("source_device_id") == self._device_id
                    else led_summary(self.hass, self._device_id)),
                "inputs": "\n".join(
                "- " + button.label + ": " + button.gesture_summary for button in self._discovery.buttons)},
            last_step=not self._configure_leds)

    async def async_step_review(self, user_input=None):
        if user_input is not None:
            try:
                self._name = user_input["name"].strip()
                # Refresh capabilities before save; a device removed or changed
                # during review must not create stale or guessed event mappings.
                try:
                    discovery = await discover_device(self.hass, self._device_id)
                except UnsupportedDeviceConfiguration as error:
                    return self._review_form(user_input, "unsupported_device_configuration", str(error))
                except ValueError:
                    return self._review_form(user_input, "device_unavailable")
                if not discovery.buttons:
                    return self._review_form(user_input, "device_unavailable")
                if not self._same_inputs(discovery):
                    self._discovery = discovery
                    return self._review_form(user_input, "device_changed")
                self._discovery = discovery
                data = discovery.build_config(self._roles(user_input), unique_id=self._identity,
                    name=self._name, load_entity_id=user_input.get("load_entity_id"))
                if self._existing.get("source_device_id") == self._device_id and self._existing.get("leds"):
                    data["leds"] = deepcopy(self._existing["leds"])
                data = enrich_native_sources(self.hass, data)
                NativeControlConfig.parse(data)
                self._pending = data
                if self._configure_leds:
                    return self._led_form()
                return self._save(data)
            except (ValueError, TypeError, KeyError):
                return self._review_form(user_input, "invalid_control")
        return self._review_form()

    async def async_step_review_leds(self, user_input=None):
        return await self.async_step_review(user_input)

    def _save(self, data):
        data = enrich_native_sources(self.hass, clean_config(data))
        parsed = NativeControlConfig.parse(data)
        require_source_dependencies(self.hass, parsed)
        entry = self._get_entry()
        if self._already_managed():
            return self.async_abort(reason="device_already_configured")
        if any(item.unique_id == self._identity and item != self._editing for item in entry.subentries.values()):
            return self.async_abort(reason="already_configured")
        if self._editing:
            return self.async_update_and_abort(entry, self._editing, data=data, title=self._name)
        return self.async_create_entry(title=self._name, data=data, unique_id=self._identity)

    def _led_form(self, configuration=None, error=None, error_code="invalid_led_mapping"):
        fields = {vol.Required("configuration", default=(configuration if configuration is not None
            else json.dumps(self._pending.get("leds", []), indent=2))): TEXT}
        return self.async_show_form(step_id="leds", data_schema=vol.Schema(fields),
            errors={"base": error_code} if error else {}, last_step=True,
            description_placeholders={"error": str(error) if error else ""})

    async def async_step_leds(self, user_input=None):
        if user_input is not None:
            try:
                leds = json.loads(user_input["configuration"])
                data = self._pending | {"leds": leds}
                NativeControlConfig.parse(data)
                try:
                    discovery = await discover_device(self.hass, self._device_id)
                except UnsupportedDeviceConfiguration as error:
                    return self._led_form(user_input["configuration"], error,
                                          error_code="unsupported_device_configuration")
                except ValueError:
                    return self._led_form(user_input["configuration"], "Choose an available device before saving.",
                                          error_code="device_unavailable")
                if not discovery.buttons or not self._same_inputs(discovery):
                    return self._led_form(user_input["configuration"], "Go back and review the device inputs again.",
                                          error_code="device_changed")
                return self._save(data)
            except MQTTNotConfigured:
                return self._led_form(user_input["configuration"], "Configure MQTT before saving this control.",
                                      error_code="mqtt_not_configured")
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                return self._led_form(user_input["configuration"], error)
        return self._led_form()
