"""Attach HA's validated device triggers through their owning integration."""

from homeassistant.components.device_automation import trigger as device_trigger
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

from ..config import DOMAIN
from ..gesture_profiles import is_physical_device_trigger


class DeviceTriggerBinding:
    """Own trigger detach callbacks, including async setup/removal races."""

    def __init__(self, hass, config, notify, unavailable):
        self.hass, self.config = hass, config
        self.notify, self.unavailable = notify, unavailable
        self._unsubs = []
        self._closed = self._active = False

    async def async_start(self):
        if not self.config.device_triggers or any(not is_physical_device_trigger(trigger.config) or trigger.config.get("domain") == "mqtt"
               for trigger in self.config.device_triggers):
            return False
        device_id = self.config.device_triggers[0].config["device_id"]
        self._unsubs.append(self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed))
        try:
            for index, trigger in enumerate(self.config.device_triggers):
                validated = await device_trigger.async_validate_trigger_config(self.hass, dict(trigger.config))
                if self._closed:
                    return False

                async def action(_variables, _context=None, *, identity=trigger.id):
                    if self._active and not self._closed:
                        self.notify(identity)

                info = {"domain": DOMAIN, "name": self.config.id, "variables": {},
                        "trigger_data": {"id": trigger.id, "idx": str(index), "alias": None}}
                unsubscribe = await device_trigger.async_attach_trigger(self.hass, validated, action, info)
                if self._closed:
                    unsubscribe()
                    return False
                self._unsubs.append(unsubscribe)
            device = dr.async_get(self.hass).async_get(device_id)
            if device is None or device.disabled_by is not None:
                self.close()
                return False
            self._active = True
            return True
        except Exception:
            # Integration errors can contain transport payloads; expose one code.
            self.close()
            return False

    @callback
    def _device_changed(self, event):
        device_id = self.config.device_triggers[0].config["device_id"]
        if event.data.get("device_id") != device_id:
            return
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or device.disabled_by is not None:
            self.close()
            self.unavailable()

    @callback
    def close(self):
        self._closed, self._active = True, False
        while self._unsubs:
            self._unsubs.pop()()
