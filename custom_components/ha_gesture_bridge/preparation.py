"""Admin-authorized, idempotent first-use enrollment and pinned source recipes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import MappingProxyType
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import EVENT_STATE_CHANGED, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .config import DOMAIN, NativeControlConfig
from .device_catalog import control_event, eligible_device, has_identifier, load_record, selected_endpoint
from .device_discovery import (
    DeviceDiscovery, DiscoveredButton, SourceMetadataUnavailable, _trigger_input,
    discover_device, valid_native_event_types,
)
from .gesture_profiles import native_action_definition

AUTOMATIC = "automatic_sources"


def default_roles(hass, discovery):
    """Known Zooz Central Scene paddle positions; keep other inputs distinct."""
    device = dr.async_get(hass).async_get(discovery.device_id)
    paddles = {"ZEN26", "ZEN27", "ZEN71", "ZEN72", "ZEN76", "ZEN77"}
    zooz = bool(device and (device.manufacturer or "").casefold() == "zooz"
                and (device.model or "").upper() in paddles
                and has_identifier(device, "zwave_js"))
    roles, assigned = {}, set()
    registry = er.async_get(hass)
    for button in discovery.buttons:
        role = "toggle" if len(discovery.buttons) == 1 else None
        source = registry.async_get(button.source_registry_id) if button.source_registry_id else None
        if zooz and source and source.platform == "zwave_js":
            # HA's Z-Wave unique ID includes the actual value ID, not a label.
            import re
            scene = re.search(r"-91-\d+-scene-(001|002)$", source.unique_id)
            if scene:
                role = {"001": "top", "002": "bottom"}[scene[1]]
        if role is None or role in assigned:
            role = button.button_id
        roles[button.button_id] = role
        assigned.add(role)
    return roles


def automatic_record(discovery, roles, *, unique_id, name, load_entity_id=None, leds=None):
    bindings = []
    for button in discovery.buttons:
        binding = {"button_id": button.button_id, "role": roles[button.button_id], "label": button.label}
        if button.source_entity_id:
            binding.update(kind="native", source_entity_id=button.source_entity_id,
                           source_registry_id=button.source_registry_id,
                           event_types=list(button.actions))
        else:
            binding.update(kind="triggers", triggers=deepcopy(list(button.triggers)))
        bindings.append(binding)
    result = {"unique_id": unique_id, "name": name, "source_device_id": discovery.device_id,
              "source_button_roles": roles, AUTOMATIC: {"version": 1, "bindings": bindings}}
    if load_entity_id:
        result["load_entity_id"] = load_entity_id
    if leds:
        result["leds"] = deepcopy(leds)
    return result


def runtime_config(hass, data):
    """Derive mappings inside pinned sources, including absent native states."""
    if AUTOMATIC not in data:
        return {key: deepcopy(value) for key, value in data.items()}
    recipe = data[AUTOMATIC]
    if recipe.get("version") != 1 or not 1 <= len(recipe.get("bindings", [])) <= 32:
        raise ValueError("invalid_automatic_sources")
    registry = er.async_get(hass)
    buttons = []
    for binding in recipe["bindings"]:
        if binding["kind"] == "native":
            source = registry.async_get(binding["source_registry_id"])
            entity_id = source.entity_id if source else binding["source_entity_id"]
            state = hass.states.get(entity_id) if source else None
            types = state.attributes.get("event_types") if state else None
            if not valid_native_event_types(types):
                types = binding["event_types"]
            provider = source.platform if source else binding.get("provider", "")
            actions = {action: native_action_definition(action, provider, types) for action in types}
            button = DiscoveredButton(binding["button_id"], binding["label"], binding["role"], actions,
                entity_id, binding["source_registry_id"])
        else:
            triggers = deepcopy(binding["triggers"])
            actions = {}
            for trigger in triggers:
                parsed = _trigger_input(trigger["config"], data["source_device_id"])
                if parsed is None:
                    raise ValueError("invalid_bound_trigger")
                actions[trigger["id"]] = parsed[3]
            button = DiscoveredButton(binding["button_id"], binding["label"], binding["role"], actions,
                triggers=tuple(triggers))
        buttons.append(button)
    discovery = DeviceDiscovery(data["source_device_id"], data["name"], tuple(buttons))
    roles = {item["button_id"]: item["role"] for item in recipe["bindings"]}
    result = discovery.build_config(roles, unique_id=data["unique_id"], name=data["name"],
                                   load_entity_id=data.get("load_entity_id"))
    if data.get("load_registry_id"):
        result["load_registry_id"] = data["load_registry_id"]
    if data.get("leds"):
        result["leds"] = deepcopy(data["leds"])
    return result


def resolve_loads(hass, data, source):
    """Explicit target authority never falls through to a physical relay."""
    result = {}
    if source:
        result["source_load"] = load_record(hass, source)
    registry = er.async_get(hass)
    if data.get("load_entity_id"):
        target = registry.async_get(data.get("load_registry_id") or data["load_entity_id"])
        device = dr.async_get(hass).async_get(target.device_id) if target and target.device_id else None
        if (target and target.domain in {"light", "switch", "input_boolean"}
                and not target.disabled_by and not (device and device.disabled_by)):
            value = load_record(hass, target)
        else:
            declared = data["load_entity_id"]
            value = {"entity_id": declared, "domain": declared.split(".", 1)[0], "available": False}
        result.update(controlled_load=value, controlled_load_origin="explicit_link")
    elif source:
        result.update(controlled_load=load_record(hass, source), controlled_load_origin="selected_endpoint")
    return result


class Preparation:
    def __init__(self, hass):
        self.hass = hass
        self.pending = {}

    def _selection(self, device_id, source_registry_id):
        device = dr.async_get(self.hass).async_get(device_id)
        if not eligible_device(self.hass, device):
            raise ServiceValidationError("device_unavailable")
        try:
            source = selected_endpoint(self.hass, device_id, source_registry_id) if source_registry_id else None
        except ValueError as error:
            raise ServiceValidationError("invalid_endpoint") from error
        return device, source

    async def prepare(self, device_id, source_registry_id=None):
        self._selection(device_id, source_registry_id)
        if device_id not in self.pending:
            task = self.hass.async_create_task(self._enroll(device_id))
            self.pending[device_id] = task
            task.add_done_callback(lambda finished: self.pending.pop(device_id, None))
        subentry = await asyncio.shield(self.pending[device_id])
        device, source = self._selection(device_id, source_registry_id)
        data = dict(subentry.data) if subentry else {}
        result = {"version": 1, "device_id": device_id,
                  "name": (device.name_by_user or device.name or "HA Device")[:128], "input_ready": False,
                  **resolve_loads(self.hass, data, source)}
        if subentry:
            gesture, eligible = control_event(self.hass, subentry.unique_id)
            if gesture is None:
                raise ServiceValidationError("preparation_failed")
            if eligible:
                state = self.hass.states.get(gesture.entity_id)
                result.update(gesture_entity_id=gesture.entity_id, control_id=subentry.unique_id,
                    input_ready=bool(state and any(item.get("ready") for item in state.attributes.get("bridge_sources", []))))
            elif "controlled_load" not in result:
                raise ServiceValidationError("device_unavailable")
        return result

    async def _enroll(self, device_id):
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if len(entries) != 1:
            raise ServiceValidationError("integration_unavailable")
        entry = entries[0]
        def current_control():
            existing = [item for item in entry.get_subentries_of_type("control")
                        if item.data.get("source_device_id") == device_id]
            if len(existing) > 1:
                raise ServiceValidationError("conflicting_controls")
            if not existing:
                return None
            return existing[0]

        if subentry := current_control():
            return subentry
        try:
            discovery = await discover_device(self.hass, device_id)
            # Manual HA setup can save this same device while a provider awaits.
            if subentry := current_control():
                return subentry
            if not discovery.buttons:
                return None
            roles = default_roles(self.hass, discovery)
            data = automatic_record(discovery, roles, unique_id=uuid4().hex,
                                    name=discovery.device_name[:119] + " Gestures")
            for binding, button in zip(data[AUTOMATIC]["bindings"], discovery.buttons, strict=True):
                if binding["kind"] == "native":
                    binding["provider"] = er.async_get(self.hass).async_get(button.source_registry_id).platform
            NativeControlConfig.parse(runtime_config(self.hass, data))
        except SourceMetadataUnavailable as error:
            raise ServiceValidationError("source_metadata_unavailable") from error
        except (ValueError, TypeError, KeyError) as error:
            raise ServiceValidationError("unsupported_device_configuration") from error
        subentry = ConfigSubentry(data=MappingProxyType(data), subentry_type="control", title=data["name"],
                                 unique_id=data["unique_id"])
        future = self.hass.loop.create_future()

        def state_added(event):
            state = event.data.get("new_state")
            if state and state.attributes.get("bridge_control_id") == data["unique_id"] and not future.done():
                future.set_result(None)

        unsubscribe = self.hass.bus.async_listen(EVENT_STATE_CHANGED, state_added)
        try:
            self.hass.config_entries.async_add_subentry(entry, subentry)
            async with asyncio.timeout(20):
                await future
        except TimeoutError as error:
            raise ServiceValidationError("preparation_timeout") from error
        finally:
            unsubscribe()
        return subentry


def async_setup_preparation(hass):
    runtime = hass.data.setdefault(DOMAIN, {})
    if "preparation" in runtime:
        return runtime["preparation"]
    manager = runtime["preparation"] = Preparation(hass)

    async def prepare(call):
        result = await manager.prepare(call.data["device_id"], call.data.get("source_registry_id"))
        return result if call.return_response else None

    identity = vol.Match(r"^[a-f0-9]{32}$")
    async_register_admin_service(hass, DOMAIN, "prepare_device", prepare,
        schema=vol.Schema({vol.Required("device_id"): identity, vol.Optional("source_registry_id"): identity}),
        supports_response=SupportsResponse.OPTIONAL)
    return manager
