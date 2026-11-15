"""Bind an event source by HA registry identity without replaying on rename."""

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event


class NativeSourceBinding:
    """Own one state subscription and one registry listener.

    Registry UUIDs are authoritative when supplied or captured. Removing that
    entry never attaches a replacement device that happens to reuse its old ID.
    A binding change calls baseline rather than replaying the source's state.
    """

    def __init__(self, hass, entity_id, registry_id, on_state, baseline):
        self.hass = hass
        self.declared_entity_id = entity_id
        self.registry_id = registry_id
        self.entity_id = None
        self._registry = er.async_get(hass)
        self._on_state, self._baseline = on_state, baseline
        self._state_unsub = self._registry_unsub = None
        self._closed = False

    @callback
    def start(self):
        self._registry_unsub = self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed)
        self._resolve(initial=True)

    @callback
    def _resolve(self, initial=False):
        entry = self._registry.async_get(self.registry_id or self.declared_entity_id)
        if entry is not None and entry.domain == "event":
            self.registry_id = entry.id
            target = entry.entity_id
        elif self.registry_id is not None:
            target = None
        else:
            target = self.declared_entity_id
        if not initial and target == self.entity_id:
            return
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        old = self.entity_id
        self.entity_id = target
        subscribe = self._baseline(target, self.hass.states.get(target) if target else None, initial, old)
        if target and subscribe is not False:
            self._state_unsub = async_track_state_change_event(self.hass, [target], self._on_state)

    @callback
    def _registry_changed(self, event):
        if self._closed:
            return
        data = event.data
        if self.registry_id is not None:
            entry = self._registry.async_get(self.registry_id)
            if data.get("entity_id") != self.entity_id and (entry is None or data.get("entity_id") != entry.entity_id):
                return
        elif data.get("entity_id") != self.declared_entity_id and data.get("old_entity_id") != self.entity_id:
            return
        self._resolve()

    @callback
    def close(self):
        self._closed = True
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        if self._registry_unsub:
            self._registry_unsub()
            self._registry_unsub = None
