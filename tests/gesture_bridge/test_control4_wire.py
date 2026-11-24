"""Export one real-HA response set for both release test suites."""
import json
import os
from pathlib import Path

from test_config_flow import install, device_fixture
from test_leds import command
from test_picker import read
from test_preparation import prepare, relay
from tools.generate_led_fixture import setup_fixture, state_fixture


async def test_control4_wire_fixture(hass):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry, "Fixture Switch")
    endpoint = relay(hass, device, "fixture_relay")
    page = await read(hass)
    fresh = await read(hass, revision=page["revision"])
    prepared = await prepare(hass, device, endpoint)

    leds = await setup_fixture(hass, configure=lambda config: config.update(source_device_id=device.id))
    await command(hass, leds, color="0000FF", brightness=80)
    feedback = state_fixture(hass, leds["aggregate"])

    responses = {"catalog": page, "unchanged": fresh, "prepared": prepared, "led_feedback": feedback}
    # Normalize only installation-specific and boot-scoped values.
    observed = json.dumps(responses, indent=2, sort_keys=True) + "\n"
    for actual, stable in ((device.id, "a" * 32), (endpoint.id, "b" * 32),
                           (prepared["control_id"], "c" * 32), (page["revision"], "d" * 64),
                           (page["native_revision"], "e" * 64)):
        observed = observed.replace(actual, stable)
    fixture = Path(__file__).parents[1] / "fixtures" / "control4-wire-v1.json"
    if os.environ.get("HA_EXPORT_CONTROL4_WIRE") == "1":
        fixture.write_text(observed)
    assert json.loads(observed) == json.loads(fixture.read_text())
