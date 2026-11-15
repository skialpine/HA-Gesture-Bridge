# HA Gesture Bridge

Normalize physical switch, dimmer and remote gestures into one Home Assistant event entity per control. Use the events in HA automations or the Control4 HA Switch/Dimmer drivers. No Control4 or DeviceSide installation is required.

This integration builds on [DeviceSide](https://github.com/DeviceSide/DeviceSide), the upstream project by [jasonmx](https://github.com/jasonmx) and its contributors. Thank you to the DeviceSide contributors for the foundation and design ideas. DeviceSide’s Git history and original MIT copyright notice are preserved. Gesture modules remain independent and can be offered back to DeviceSide later. See [upstream attribution and ancestry](UPSTREAM.md).

## Run alongside DeviceSide

Both integrations can be installed at once. DeviceSide retains its `deviceside` domain and command services; this integration owns `ha_gesture_bridge` controls, events and its catalog. It does not register or remove DeviceSide services, patch DeviceSide, run dimming loops or automatically operate linked loads. They interact only through automations you explicitly configure. Avoid assigning the same gesture/load response in two automations if you want only one action.

## Install

Requires **Home Assistant 2026.11.1 or newer**. This is a development integration; fixture coverage and hardware evidence are listed separately below.

### Marketplace / HACS custom repository

Add `https://github.com/skialpine/HA-Gesture-Bridge` as a custom repository of type **Integration**, download **HA Gesture Bridge**, then restart Home Assistant. Go to **Settings > Devices & services > Add integration > HA Gesture Bridge**.

It is a custom repository, not a claim of inclusion in the default Marketplace catalog. Native C4 discovery and ordinary direct switch/dimmer control continue to work without this integration.

### Manual installation

Build `dist/ha_gesture_bridge.zip` with `python3 tools/build_integration.py`. Extract it inside HA's `custom_components` directory, producing `custom_components/ha_gesture_bridge/manifest.json`. Restart HA after installation, then add the integration as above. Do not install the repository's tests or C4 driver packages into HA.

## Configure controls

Add the installation once, then add a **Control** from its integration card. Choose a name, source mode and optional linked load (`switch`, `input_boolean` or `light`). The UI assigns an immutable control identity. For native event sources, select the top/bottom event entities. The native profile uses event types those entities actually advertise; manual mappings are available for other names and buttons.

Advanced source configuration covers native/shared event entities, raw Z-Wave Central Scene, ZHA, MQTT/Zigbee2MQTT and other HA bus events. See [source mappings](docs/sources.md). MQTT uses HA's configured MQTT integration; the bridge never opens a second broker connection. No experimental Zigbee2MQTT event entities are required.

The linked load supplies independent state/capability snapshots. A gesture does not automatically switch it. Controls without loads are supported. Physical smart-bulb/relay decoupling remains a device setting; the integration does not change it.

### Existing development YAML

Existing `event: - platform: ha_gesture_bridge` configurations remain supported. Keep the same unique control ID and top/bottom IDs when expanding a control or importing it through the UI. The import path preserves the aggregate entity registry identity and avoids duplicate subscriptions. After importing, remove the corresponding legacy YAML entry when convenient; do not leave a second independently configured control for the same physical input.

## Events and capabilities

A control publishes reported single/double/triple/higher taps, hold start/repeat, release and mapped custom actions. Only configured source-supported gestures are declared. Completed taps do not imply physical pushed/released edges. Restored events, retained MQTT actions, availability changes and linked-load updates are snapshots; they never generate a fresh physical gesture.

The original switch/input_boolean contract remains **1.0**. Light-linked controls use additive **1.1** metadata for brightness and light capabilities. Stable control/button/gesture IDs preserve consumer programming across display changes. See [wire contract](docs/gesture-contract.md) and [catalog](docs/catalog.md).

Delivery is live and best effort. Gestures during an outage are not replayed. Missing release reporting cannot be repaired by inventing releases; timeout cleanup clears hold bookkeeping only.

## Automations and DeviceSide

[Examples](docs/automations.md) show double-tap scene handling and separate hold/release automations calling DeviceSide's `raise_brightness`, `lower_brightness` and `stop` for supported targets. DeviceSide remains optional; installing this bridge does not extend its device compatibility. For a light already represented in Control4, the C4 consumer can instead forward through its Button Link connections.

## Compatibility evidence

| Interface | Automated evidence | Physical evidence |
| --- | --- | --- |
| Native HA event entities | Repeated presses, separate/shared sources, restore, availability and linked-load snapshots | CHV Zooz ZEN76 upper double/triple reached the original HA proof; expanded UI/mappings not yet live-qualified |
| Z-Wave Central Scene | Manufacturer-independent action/filter fixtures | Pending |
| ZHA | Explicit command/argument fixtures | Pending |
| MQTT / Zigbee2MQTT | Fresh/retained messages, payload mapping and availability fixtures | Pending |
| Generic HA bus | Declarative identity/action mapping fixtures | Pending |

Declared capabilities and passing fixtures do not verify every model, firmware or radio implementation. Collect diagnostics from the integration card for source readiness/configuration problems; exported diagnostics omit household identifiers and payloads.

## Development

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m tools.validate_gesture_contract
.venv/bin/python tools/build_integration.py
```

HA-only checks need no consumer checkout. To run actual producer publications through C4's Lua consumers, explicitly set `C4_HA_SWITCH_CHECKOUT=/path/to/Control4-HA-Switch` and install LuaJIT and/or Lua. The consumer repository retains its own schema/fixtures, Lua runtime checks and packaging checks.

The original DeviceSide checkout, existing C4 identities and Coordinator family APIs are unchanged. See [contributing](CONTRIBUTING.md) for stable identity and source-adapter rules.

The upstream [DeviceSide audit-code skill](.agents/skills/audit-code/SKILL.md) is included for development reviews.
