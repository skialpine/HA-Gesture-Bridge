"""Standard Indicator discovery and direct commands using actual Z-Wave models."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.exceptions import ServiceValidationError
import pytest
from zwave_js_server.const import CommandClass
from zwave_js_server.event import Event
from zwave_js_server.model.node import Node

from custom_components.ha_gesture_bridge import zwave_indicator as indicator
from custom_components.ha_gesture_bridge.leds import async_setup_leds


def value(indicator_id=0x43, property_id=1, *, endpoint=0, metadata=None, version=3, current=42):
    return {"commandClass": 135, "commandClassName": "Indicator", "endpoint": endpoint,
        "property": indicator_id, "propertyName": "Button indicator", "propertyKey": property_id,
        "ccVersion": version, "value": current,
        "metadata": metadata or {"type": "number", "readable": True, "writeable": True, "min": 0, "max": 99}}


def node(values):
    endpoints = sorted({item.get("endpoint", 0) for item in values})
    client = SimpleNamespace(connected=True, driver=object())
    result = Node(client, {"nodeId": 7, "index": 7, "status": 4, "ready": True,
        "values": values, "endpoints": [{"nodeId": 7, "index": endpoint,
            "commandClasses": [{"id": 135, "name": "Indicator", "version": 3, "isSecure": True}]}
            for endpoint in endpoints]})
    return result


def configure(hass, monkeypatch, values=None):
    runtime = node(values or [value()])
    current = [runtime]
    monkeypatch.setattr(indicator, "_resolve_node", lambda _hass, _device: current[0])
    changed = []
    owner = SimpleNamespace(config=SimpleNamespace(control_id="fixture"), entity_id="event.fixture")
    control = indicator.ZWaveIndicatorController(hass, owner, "physical-device", lambda: changed.append(control.snapshot()))
    return control, runtime, current, changed


async def settle(hass, control):
    if control._task:
        await control._task
    await hass.async_block_till_done()


async def test_standard_discovery_ignores_unsafe_and_non_indicator_values(hass, monkeypatch):
    values = [value(), value(0x43, 2, metadata={"type": "boolean", "readable": True, "writeable": True}, current=False),
        value(0x44, endpoint=1), value(0x50), value(0x60), value(0x80), value(0xF0), value(0x43, 3),
        value(0x43, 9), value(0x01), value(0x45, metadata={"type": "number", "readable": True, "writeable": False, "min": 0, "max": 99})]
    values.append(value() | {"commandClass": 37})
    control, runtime, _current, _changed = configure(hass, monkeypatch, values)
    channels = indicator.discover_indicators(hass, "physical-device")
    assert [(row.endpoint, row.indicator) for row in channels] == [(0, 0x43), (1, 0x44)]
    assert channels[0].level_id and channels[0].binary_id
    assert "2 standard" in indicator.indicator_summary(hass, "physical-device")
    runtime.values = {key: row for key, row in runtime.values.items() if row.property_ == 0x50}
    assert indicator.discover_indicators(hass, "physical-device") == ()
    assert "identification blinking only" in indicator.indicator_summary(hass, "physical-device")
    control.close()


async def test_readonly_number_ranges_and_legacy_versions_are_not_guessed(hass, monkeypatch):
    raw = value("value", None, version=3, current=None,
        metadata={"type": "number", "readable": True, "writeable": True, "min": 0, "max": 255})
    _control, runtime, _current, _changed = configure(hass, monkeypatch, [raw])
    assert not indicator.discover_indicators(hass, "physical-device")
    legacy = next(iter(runtime.values.values()))
    legacy.data["ccVersion"] = 1
    channels = indicator.discover_indicators(hass, "physical-device")
    assert len(channels) == 1 and channels[0].legacy_id and not channels[0].level_id
    values = [value(metadata={"type": "number", "readable": True, "writeable": True, "min": 0, "max": 99,
                             "allowed": [{"from": 0, "to": 99, "step": 2}]})]
    runtime.values.clear()
    runtime.update({"nodeId": 7, "index": 7, "status": 4, "ready": True, "values": values, "endpoints": []})
    assert not indicator.discover_indicators(hass, "physical-device")


async def test_setup_reads_only_and_unknown_never_uses_optimistic_cache(hass, monkeypatch):
    control, runtime, _current, changed = configure(hass, monkeypatch)
    invoke = runtime.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=None)
    control.start()
    try:
        await settle(hass, control)
        assert invoke.await_args.args[:2] == (CommandClass.INDICATOR, "get")
        assert all(call.args[1] == "get" for call in invoke.await_args_list)
        snapshot = control.snapshot()[0]
        assert snapshot["available"] and snapshot["colors"] == ["000000", "FFFFFF"]
        assert "color" not in snapshot and "brightness" not in snapshot
        assert changed
    finally:
        control.close()


@pytest.mark.parametrize("previous_complete", [False, True])
async def test_failed_indicator_read_recovers_on_existing_poll_without_writes(hass, monkeypatch, previous_complete):
    polls = []
    monkeypatch.setattr(indicator, "async_track_time_interval",
        lambda _hass, callback, _interval: polls.append(callback) or (lambda: None))
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    calls, fail_read = [], [not previous_complete]
    report = [{"indicatorId": 67, "propertyId": 1, "value": 99}]

    async def invoke(cc, method, *args, **kwargs):
        calls.append(method)
        if fail_read[0]:
            fail_read[0] = False
            raise TimeoutError("simulated radio timeout")
        return deepcopy(report)

    runtime.endpoints[0].async_invoke_cc_api = invoke
    control.start()
    try:
        await settle(hass, control)
        if previous_complete:
            assert control.snapshot()[0]["color"] == "FFFFFF"
            fail_read[0] = True
            control._node_changed({"value": next(iter(runtime.values.values()))})
            await settle(hass, control)
        assert control._confirmed["indicator_0_67"] == {}
        assert not {"color", "behavior", "brightness"} & control.snapshot()[0].keys()
        assert control.snapshot()[0]["available"]
        report[0]["value"] = 0
        calls.clear()
        assert len(polls) == 1
        polls[0](None)
        await settle(hass, control)
        assert calls == ["get"]
        assert control.snapshot()[0]["color"] == "000000"
        assert control.snapshot()[0]["behavior"] == "always_off"
        assert control.snapshot()[0]["brightness"] == 0
        assert len(polls) == 1
    finally:
        control.close()


async def test_retired_owner_read_cannot_clear_replacement_confirmation(hass, monkeypatch):
    original, runtime, _current, changed = configure(hass, monkeypatch)
    pending, entered = asyncio.Event(), asyncio.Event()
    reads = 0

    async def invoke(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await pending.wait()
        return [{"indicatorId": 67, "propertyId": 1, "value": 99}]

    runtime.endpoints[0].async_invoke_cc_api = invoke
    original.start()
    old_read = original._task
    await entered.wait()
    replacement_changes = []
    replacement = indicator.ZWaveIndicatorController(hass, original.owner, "physical-device",
        lambda: replacement_changes.append(replacement.snapshot()))
    replacement.start()
    try:
        await replacement._task
        assert replacement.snapshot()[0]["color"] == "FFFFFF"
        before = len(changed)
        replacement_before = len(replacement_changes)
        pending.set()
        await old_read
        assert len(changed) == before
        assert len(replacement_changes) == replacement_before
        assert replacement.snapshot()[0]["color"] == "FFFFFF"
        assert async_setup_leds(hass).controllers["fixture"] is replacement
    finally:
        pending.set()
        original.close()
        replacement.close()


async def test_direct_api_set_and_get_publish_actual_readback_only(hass, monkeypatch):
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    report = [{"indicatorId": 0x43, "propertyId": 1, "value": 10}]
    calls = []

    async def invoke(cc, method, *args, **kwargs):
        calls.append((cc, method, args, kwargs))
        return deepcopy(report) if method == "get" else {"status": 255}

    runtime.endpoints[0].async_invoke_cc_api = invoke
    runtime.async_set_value = AsyncMock(side_effect=AssertionError("Optimistic setter must never be used"))
    control.start()
    try:
        await settle(hass, control)
        calls.clear()
        await control.set_led("indicator_0_67", "FFFFFF", 60, behavior="always_on")
        assert calls == [(CommandClass.INDICATOR, "set", ([{"indicatorId": 67, "propertyId": 1, "value": 59}],), {"wait_for_result": True}),
                         (CommandClass.INDICATOR, "get", (67,), {"wait_for_result": True})]
        assert control.snapshot()[0]["brightness"] == 10  # Actual device report, not requested 60%.
        assert control.snapshot()[0]["color"] == "FFFFFF"
        runtime.async_set_value.assert_not_awaited()
    finally:
        control.close()


@pytest.mark.parametrize("color,brightness", [("FF0000", None), ("000000", 20), (None, -1), (None, True), (None, float("nan")), (None, None)])
async def test_invalid_actions_have_no_writes(hass, monkeypatch, color, brightness):
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    invoke = runtime.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=None)
    control.start()
    try:
        await settle(hass, control)
        invoke.reset_mock()
        with pytest.raises(ServiceValidationError):
            await control.set_led("indicator_0_67", color, brightness)
        invoke.assert_not_awaited()
    finally:
        control.close()


async def test_reconnect_rebinds_and_metadata_removal_revokes_authority(hass, monkeypatch):
    control, original, current, _changed = configure(hass, monkeypatch)
    original.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=[{"indicatorId": 67, "propertyId": 1, "value": 99}])
    control.start()
    try:
        await settle(hass, control)
        assert control.snapshot()[0]["brightness"] == 100
        original.client.connected = False
        control._refresh()
        assert not control.snapshot()[0]["available"] and "color" not in control.snapshot()[0]
        assert not any(original._listeners.values())
        replacement = current[0] = node([value(0x43, 2, metadata={"type": "boolean", "readable": True, "writeable": True})])
        replacement.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=[{"indicatorId": 67, "propertyId": 2, "value": 0}])
        control._refresh()
        await settle(hass, control)
        assert control.snapshot()[0]["color"] == "000000" and not control.snapshot()[0]["brightness_supported"]
        calls = replacement.endpoints[0].async_invoke_cc_api
        calls.reset_mock()
        await control.set_led("indicator_0_67", "FFFFFF", behavior="always_on")
        assert calls.await_args_list[0].args == (CommandClass.INDICATOR, "set", [{"indicatorId": 67, "propertyId": 2, "value": True}])
        row = next(iter(replacement.values.values()))
        row.metadata.data["writeable"] = False
        replacement.emit("metadata updated", {"value": row})
        assert control.snapshot() == []
        with pytest.raises(ServiceValidationError):
            await control.set_led("indicator_0_67", "FFFFFF", behavior="always_on")
    finally:
        control.close()
    assert not any(replacement._listeners.values())
    assert "fixture" not in async_setup_leds(hass).controllers


async def test_failed_supervision_and_unload_stop_followup_read(hass, monkeypatch):
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    invoke = runtime.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=None)
    control.start()
    await settle(hass, control)
    invoke.reset_mock()
    invoke.return_value = {"status": 2}
    with pytest.raises(ServiceValidationError, match="rejected"):
        await control.set_led("indicator_0_67", "FFFFFF", behavior="always_on")
    assert invoke.await_count == 1

    async def unloading(*args, **kwargs):
        control.close()
        return {"status": 255}

    invoke.side_effect = unloading
    invoke.reset_mock()
    with pytest.raises(ServiceValidationError, match="reconfigured"):
        await control.set_led("indicator_0_67", "FFFFFF", behavior="always_on")
    assert invoke.await_count == 1


async def test_dead_node_unknown_and_newly_discovered_channels(hass, monkeypatch):
    control, runtime, _current, _changed = configure(hass, monkeypatch, [value(0x50)])
    runtime.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=None)
    control.start()
    try:
        assert control.snapshot() == []
        runtime.receive_event(Event("value added", {"source": "node", "event": "value added", "nodeId": 7, "args": value()}))
        await settle(hass, control)
        assert len(control.snapshot()) == 1
        for status in (0, 3):
            runtime.data["status"] = status
            control._refresh()
            assert not control.snapshot()[0]["available"]
            with pytest.raises(ServiceValidationError):
                await control.set_led("indicator_0_67", "FFFFFF", behavior="always_on")
        runtime.data["status"] = 4
        control._refresh()
        await settle(hass, control)
        assert control.snapshot()[0]["available"]
    finally:
        control.close()


async def test_non_zwave_device_has_no_optional_import(hass, monkeypatch):
    assert indicator.indicator_summary(hass, "missing") == "No standard Z-Wave status LED controls detected."


async def test_brightness_only_preserves_power_and_binary_off_wins(hass, monkeypatch):
    control, runtime, _current, _changed = configure(hass, monkeypatch, [value(),
        value(0x43, 2, metadata={"type": "boolean", "readable": True, "writeable": True})])
    report = [{"indicatorId": 67, "propertyId": 1, "value": 50},
              {"indicatorId": 67, "propertyId": 2, "value": 0}]
    calls = []

    async def invoke(cc, method, *args, **kwargs):
        calls.append(method)
        return deepcopy(report) if method == "get" else {"status": 255}

    runtime.endpoints[0].async_invoke_cc_api = invoke
    control.start()
    try:
        await settle(hass, control)
        assert control.snapshot()[0]["color"] == "000000"
        calls.clear()
        with pytest.raises(ServiceValidationError, match="explicit"):
            await control.set_led("indicator_0_67", brightness=80)
        assert calls == ["get"]
        report[1]["value"] = 255
        calls.clear()
        await control.set_led("indicator_0_67", brightness=80)
        assert calls == ["get", "set", "get"]
        calls.clear()
        with pytest.raises(ServiceValidationError, match="explicit"):
            await control.set_led("indicator_0_67", brightness=0)
        assert calls == ["get"]
    finally:
        control.close()


async def test_unload_pending_read_never_notifies_after_removal(hass, monkeypatch):
    control, runtime, _current, changed = configure(hass, monkeypatch)
    pending = asyncio.Event()

    async def invoke(*args, **kwargs):
        await pending.wait()
        return [{"indicatorId": 67, "propertyId": 1, "value": 99}]

    runtime.endpoints[0].async_invoke_cc_api = invoke
    control.start()
    task = control._task
    await asyncio.sleep(0)
    control.close()
    before = len(changed)
    pending.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(changed) == before
    control._notify()
    assert len(changed) == before


@pytest.mark.parametrize("requested_color", [None, "FFFFFF"])
@pytest.mark.parametrize("brightness", [0.1, 0.5])
async def test_positive_fraction_rounding_to_zero_never_silently_turns_off(hass, monkeypatch, requested_color, brightness):
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    invoke = runtime.endpoints[0].async_invoke_cc_api = AsyncMock(
        return_value=[{"indicatorId": 67, "propertyId": 1, "value": 50}])
    control.start()
    try:
        await settle(hass, control)
        invoke.reset_mock()
        with pytest.raises(ServiceValidationError):
            await control.set_led("indicator_0_67", requested_color, brightness,
                                  behavior="always_on" if requested_color == "FFFFFF" else None)
        assert all(call.args[1] == "get" for call in invoke.await_args_list)
        assert control.snapshot()[0]["color"] == "FFFFFF"
    finally:
        control.close()


@pytest.mark.parametrize("behavior", ["always_on", "always_off"])
async def test_standard_explicit_behavior_changes_power_without_color(hass, monkeypatch, behavior):
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    invoke = runtime.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=None)
    control.start()
    try:
        await settle(hass, control)
        invoke.reset_mock()
        await control.set_led("indicator_0_67", behavior=behavior)
        assert invoke.await_args_list[0].args == (CommandClass.INDICATOR, "set", [
            {"indicatorId": 67, "propertyId": 1, "value": 99 if behavior == "always_on" else 0}])
        assert control.snapshot()[0]["color_supported"] is False
        assert control.snapshot()[0]["behaviors"] == ["always_on", "always_off"]
    finally:
        control.close()


@pytest.mark.parametrize("color,behavior", [("FFFFFF", None), ("000000", None), (None, "follow_load"),
    ("FFFFFF", "always_off"), ("000000", "always_on")])
async def test_standard_color_alone_never_changes_indicator_power(hass, monkeypatch, color, behavior):
    control, runtime, _current, _changed = configure(hass, monkeypatch)
    invoke = runtime.endpoints[0].async_invoke_cc_api = AsyncMock(return_value=None)
    control.start()
    try:
        await settle(hass, control)
        invoke.reset_mock()
        with pytest.raises(ServiceValidationError):
            await control.set_led("indicator_0_67", color, behavior=behavior)
        invoke.assert_not_awaited()
    finally:
        control.close()
