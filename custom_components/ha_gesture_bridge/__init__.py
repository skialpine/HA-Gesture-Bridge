"""Config-entry lifecycle for the independent HA Gesture Bridge integration."""

from __future__ import annotations

from copy import deepcopy

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er

from .config import DOMAIN, NativeControlConfig
from .device_discovery import valid_native_event_types
from .entity_binding import RegistryEntityBinding
from .latency import async_setup_latency
from .leds import async_setup_leds

PLATFORMS = [Platform.EVENT, Platform.SENSOR]


def watch_native_metadata(hass, entry, binding):
    """Regenerate a pinned recipe on capability changes, following registry renames."""
    expected = binding["event_types"]

    def inspect(state, initial=False):
        nonlocal expected
        types = state.attributes.get("event_types") if state else None
        if not valid_native_event_types(types):
            return
        types = list(types)
        changed = expected != types
        expected = types
        if changed and not initial:
            hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

    def baseline(target, state, initial, old):
        inspect(state, initial)

    @callback
    def state_changed(event):
        inspect(event.data.get("new_state"))

    watcher = RegistryEntityBinding(hass, binding["source_entity_id"], binding["source_registry_id"],
        state_changed, baseline, frozenset({"event"}))
    entry.async_on_unload(watcher.close)
    watcher.start()


def enrich_native_sources(hass: HomeAssistant, data: dict) -> dict:
    """Persist source registry identity and resolve current names for editing.

    Existing registry UUIDs take precedence over text IDs: a deleted source
    must never silently attach to a different entity reusing its former name.
    """
    result = deepcopy(data)
    registry = er.async_get(hass)
    records = [result, *result.get("buttons", []), *result.get("sources", [])]
    for record in records:
        if not isinstance(record, dict) or "source_entity_id" not in record:
            continue
        if source_id := record.get("source_registry_id"):
            if not isinstance(source_id, str):
                continue  # The parser reports an invalid registry identity.
            source = registry.async_get(source_id)
            if source is not None:
                record["source_entity_id"] = source.entity_id
        elif isinstance(record["source_entity_id"], str) and (source := registry.async_get(record["source_entity_id"])):
            record["source_registry_id"] = source.id
    for led in result.get("leds", []) if isinstance(result.get("leds", []), list) else []:
        if not isinstance(led, dict):
            continue
        for kind in ("color", "power", "brightness"):
            endpoint = led.get(kind)
            if not isinstance(endpoint, dict):
                continue
            if identity := endpoint.get("registry_id"):
                if isinstance(identity, str) and (target := registry.async_get(identity)):
                    endpoint["entity_id"] = target.entity_id
            elif isinstance(endpoint.get("entity_id"), str) and (target := registry.async_get(endpoint["entity_id"])):
                endpoint["registry_id"] = target.id
    return result


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Retain the optional existing YAML event platform."""
    hass.data.setdefault(DOMAIN, {}).setdefault("yaml_controls", {})
    async_setup_latency(hass)
    async_setup_leds(hass)
    from .preparation import async_setup_preparation
    async_setup_preparation(hass)
    from .picker import async_setup_picker
    async_setup_picker(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load control subentries and the installation-wide discovery catalog."""
    runtime = hass.data.setdefault(DOMAIN, {})
    controls = runtime.setdefault("yaml_controls", {})
    registry = er.async_get(hass)
    parsed_controls = []
    for subentry in entry.get_subentries_of_type("control"):
        try:
            data = enrich_native_sources(hass, dict(subentry.data))
            from .preparation import runtime_config
            control = NativeControlConfig.parse(runtime_config(hass, data))
        except (ValueError, TypeError) as error:
            raise ConfigEntryError(f"Invalid gesture control {subentry.title}: {error}") from error
        parsed_controls.append((subentry, control, data))
    # Validate the whole entry before removing any functioning legacy entity.
    # A damaged second control must not partially migrate the first control.
    for subentry, control, data in parsed_controls:
        if data != dict(subentry.data):
            hass.config_entries.async_update_subentry(entry, subentry, data=data)
        # The old YAML object must relinquish its state/listeners before the UI
        # platform adds the same unique ID. Retain the registry's actual entity
        # ID so dashboards and existing integrations keep their references.
        legacy = controls.get(control.control_id)
        if legacy and not legacy.get("imported"):
            entity = legacy.get("entity")
            if entity is not None and entity.entity_id and hass.states.get(entity.entity_id) is not None:
                await entity.async_remove()
            legacy["imported"] = True
        entity_id = registry.async_get_entity_id("event", DOMAIN, control.control_id)
        if entity_id and registry.async_get(entity_id).config_entry_id in (None, entry.entry_id):
            registry.async_update_entity(entity_id, config_entry_id=entry.entry_id,
                                         config_subentry_id=subentry.subentry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    # Only automatic native inputs need mapping regeneration. Physical presses
    # keep the same event_types and do not reload or query a provider.
    from .preparation import AUTOMATIC
    bindings = [binding for subentry in entry.get_subentries_of_type("control")
                for binding in subentry.data.get(AUTOMATIC, {}).get("bindings", [])
                if binding.get("kind") == "native"]
    for binding in bindings:
        watch_native_metadata(hass, entry, binding)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply add/edit/remove updates with complete platform listener cleanup."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload all listeners, entities and catalog observers for this entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
