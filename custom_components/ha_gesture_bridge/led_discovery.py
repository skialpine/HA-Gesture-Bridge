"""Choose a device LED backend while keeping the consumer contract shared."""

from __future__ import annotations

from .led_profiles import profile_for_device, profile_summary
from .zwave_indicator import ZWaveIndicatorController, indicator_summary
from .zwave_parameters import ZWaveParameterLEDController


def led_summary(hass, device_id: str) -> str:
    """Describe cached capabilities without changing device configuration."""
    if profile_for_device(hass, device_id) is not None:
        return profile_summary(hass, device_id)
    return indicator_summary(hass, device_id)


def create_led_controller(hass, owner, device_id: str, changed):
    """Select a backend even when its existing HA transport is not ready yet.

    A known device profile must still validate its live parameter metadata.
    It never falls through to a guessed parameter or a second LED writer.
    """
    if (profile := profile_for_device(hass, device_id)) is not None:
        return ZWaveParameterLEDController(hass, owner, device_id, changed, profile)
    return ZWaveIndicatorController(hass, owner, device_id, changed)
