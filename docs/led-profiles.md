# Adding an LED profile

HA Gesture Bridge reads the firmware-resolved settings already provided by Home Assistant's Z-Wave JS client. It does not open a second Z-Wave connection, parse downloaded source JSON at runtime or enable configuration entities.

## Metadata available from HA

`node.get_configuration_values()` exposes the actual configuration values. `node.device_config.param_information` contains the resolved device definition. Value metadata includes the label, complete option table, numeric range, byte size, read/write flags, configuration provenance and optional semantic `purpose`.

Z-Wave JS currently has no LED/color/mode entries in its parameter-purpose taxonomy. A label containing “LED” is insufficient authority to write a parameter. Profiles therefore bind a hardware fingerprint to reviewed schemas, then require the live metadata to match. Values with a contradictory purpose, missing configuration provenance, partial/bitmasked identities or changed options are rejected.

The HA `zwave_js/get_config_parameters` WebSocket projection omits `valueSize`, `isFromConfig` and `purpose`; runtime discovery uses the native value objects instead.

## Shared profile structure

`led_discovery.py` selects a reviewed profile or standard Indicator support. Each `LEDProfile` declares hardware fingerprints and one or more `LEDChannelProfile` entries. A channel requires a mode schema; color and brightness are optional. Reviewed alternative option tables support firmware variants, including ZEN32 palettes and brightness levels.

The initial shared enum schemas support:

- Zooz ZEN26/27/78 fixed-color on/off indicators.
- Zooz ZEN71/72/76/77 shared color indicators.
- Zooz ZEN30's two indicators and ZEN32's five indicators.
- GE/Enbrighten 46201 / ZW4008 and Jasco 46562 / ZW4008, which reuse the same upstream four-option mode template.

Existing Office identity remains `status` / `toggle`. Multi-button scene indicators associate with the selected control's actual native button identities where HA exposes an unambiguous Z-Wave scene mapping. Unmatched channels retain their own stable keys. No physical LED is invented for a paddle, and no indicator is duplicated across two channels.

## Add another compatible device

1. Review the manufacturer's documentation and Z-Wave JS device definition. Record manufacturer ID, product type, product ID and relevant firmware differences.
2. Read the device's actual metadata, including the complete option tables and parameter sizes. Reuse an existing reviewed schema where it is identical; do not copy numbers from a similar model.
3. Add an `LEDProfile` declaration with the hardware fingerprints, explicit registry aliases and channel definitions. Add color/brightness variants only where the device reports those options. The shared `ZWaveParameterLEDController` handles transport, locking, reconnects, validation and confirmed readback.
4. Test exact raw parameter writes, unrelated parameters remaining untouched, schema mismatch, missing capabilities, unknown readback and unload. Record physical qualification separately from simulated commands.

A different command layout needs an adapter behind the same factory. HomeSeer global status-mode gates and Inovelli hue/packed notification values do not fit the simple independent enum channels; do not approximate them as this profile type. The standard Indicator path remains available where its advertised visual controls suffice.

Discovery and startup perform reads only. Profiles never change relays, smart-bulb mode or scene reporting automatically. Gesture normalization and the Control4 wire contract remain independent of vendor definitions.

Sources: [Z-Wave JS device definitions](https://github.com/zwave-js/zwave-js/tree/master/packages/config/config/devices), [parameter purposes](https://github.com/zwave-js/zwave-js/blob/master/docs/config-files/parameter-purpose.md), [Python metadata model](https://github.com/home-assistant-libs/zwave-js-server-python/blob/0.74.0/zwave_js_server/model/value.py).
