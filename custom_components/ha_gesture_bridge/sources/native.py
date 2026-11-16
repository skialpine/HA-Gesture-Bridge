"""Bind native event input through the shared registry identity boundary."""

from ..entity_binding import RegistryEntityBinding


class NativeSourceBinding(RegistryEntityBinding):
    def __init__(self, hass, entity_id, registry_id, on_state, baseline):
        super().__init__(hass, entity_id, registry_id, on_state, baseline, frozenset({"event"}))
