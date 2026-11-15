"""Allowlisted anonymous diagnostics for the gesture bridge installation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .config import DOMAIN, NativeControlConfig

_SOURCE_TYPES = frozenset({"native_event", "shared_native", "zwave_js", "zha", "mqtt", "bus"})


def _count(value: Any) -> int:
    """Never leak arbitrary upstream values through numeric diagnostic fields."""
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else 0


def _source_configuration(data: Mapping[str, Any]) -> tuple[int, dict[str, int]]:
    sources = data.get("sources")
    if isinstance(sources, list):
        kinds = Counter()
        for source in sources:
            kind = source.get("type") if isinstance(source, Mapping) else None
            kinds[kind if isinstance(kind, str) and kind in _SOURCE_TYPES else "other"] += 1
        return len(sources), dict(sorted(kinds.items()))
    buttons = data.get("buttons")
    count = len(buttons) if isinstance(buttons, list) else int("source_entity_id" in data)
    return count, {"native_event": count} if count else {}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    """Return counts and fixed status categories; omit raw configuration entirely.

    Names, entity/device/control/session IDs, topics, payloads, mapping match
    values, arbitrary error text, and credentials never enter this result. This
    is intentionally an allowlist rather than recursive key-name redaction.
    """
    controls: list[Mapping[str, Any]] = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type == "control" and isinstance(subentry.data, Mapping):
            controls.append(subentry.data)
    # Keep diagnostics useful for development YAML controls during UI import.
    runtime = hass.data.get(DOMAIN, {})
    if isinstance(runtime, dict) and isinstance(runtime.get("yaml_controls"), Mapping):
        for item in runtime["yaml_controls"].values():
            if isinstance(item, dict) and isinstance(item.get("config"), Mapping):
                controls.append(item["config"])
    unique_controls: dict[str, Mapping[str, Any]] = {}
    unidentified = []
    for data in controls:
        identity = data.get("unique_id")
        if isinstance(identity, str):
            # UI subentries are current; retained YAML is only a fallback for
            # controls not managed by the installation entry.
            unique_controls.setdefault(identity, data)
        else:
            unidentified.append(data)
    controls = [*unique_controls.values(), *unidentified]
    configured_sources = 0
    source_types: Counter = Counter()
    load_domains: Counter = Counter()
    configuration_errors = 0
    for data in controls:
        count, kinds = _source_configuration(data)
        configured_sources += count
        source_types.update(kinds)
        target = data.get("load_entity_id")
        if isinstance(target, str):
            domain = target.split(".", 1)[0]
            load_domains[domain if domain in {"switch", "input_boolean", "light"} else "other"] += 1
        try:
            NativeControlConfig.parse(dict(data))
        except (ValueError, TypeError):
            configuration_errors += 1
    source_availability: Counter = Counter({"available": 0, "unavailable": 0, "unknown": 0})
    aggregate_count = ready_sources = source_errors = unmapped_count = error_count = 0
    for registry_entry in er.async_get(hass).entities.values():
        if registry_entry.platform != DOMAIN or not registry_entry.entity_id.startswith("event."):
            continue
        state = hass.states.get(registry_entry.entity_id)
        if state is None:
            continue
        attrs = state.attributes
        identity = attrs.get("bridge_control_id")
        if not isinstance(identity, str) or identity not in unique_controls:
            continue
        aggregate_count += 1
        unmapped_count += _count(attrs.get("bridge_unmapped_count"))
        error_count += _count(attrs.get("bridge_error_count"))
        sources = attrs.get("bridge_sources")
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, Mapping):
                continue
            ready_sources += int(source.get("ready") is True)
            availability = source.get("availability")
            source_availability[availability if isinstance(availability, str) and availability in source_availability else "unknown"] += 1
            source_errors += int(bool(source.get("error")))
    return {
        "integration": {"domain": DOMAIN, "diagnostics_format": 1},
        "configuration": {
            "controls": len(controls), "sources": configured_sources,
            "source_types": dict(sorted(source_types.items())),
            "linked_load_domains": dict(sorted(load_domains.items())),
            "invalid_controls": configuration_errors,
        },
        "runtime": {
            "aggregates": aggregate_count, "ready_sources": ready_sources,
            "source_availability": dict(sorted(source_availability.items())),
            "source_errors": source_errors, "unmapped_actions": unmapped_count,
            "errors": error_count,
        },
    }
