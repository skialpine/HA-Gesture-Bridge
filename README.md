# HA Gesture Bridge

Pick a switch, dimmer or remote from Home Assistant and make its reported button gestures available to HA automations and compatible Control4 drivers. Each configured device gets one gesture event entity.

**Requires Home Assistant 2026.11.1 or newer.** Connect the device to HA first and enable its scene or button reporting if required by its manufacturer.

## Install

1. Open **Settings → Marketplace → Add from link**.
2. Paste `https://github.com/skialpine/HA-Gesture-Bridge`.
3. Install **HA Gesture Bridge** and **restart Home Assistant**.
4. Open **Settings → Devices & services → Integrations → Add integration**.
5. Search for **HA Gesture Bridge** and add it.

<details>
<summary>Manual installation</summary>

Copy this repository’s `custom_components/ha_gesture_bridge` folder into your HA configuration directory, so `/config/custom_components/ha_gesture_bridge/manifest.json` exists. Restart HA, then follow steps 4–5 above.

</details>

## Select a device

With the matching HA Coordinator, HA Switch or HA Light drivers, select the
physical switch/light/remote in Composer **Actions → Select Device**. HA prepares
its gestures and supported LEDs on first use. Browsing creates no gesture
entities, attaches no input listeners and changes no device parameters.
Ordinary loads without physical gestures use their existing entity directly.

The read-only `ha_gesture_bridge.get_devices` action supplies a bounded,
versioned device list to the Coordinator. Subsequent openings can send its
revision to receive an unchanged confirmation instead of the list. It includes
native HA light groups and excludes devices exported from Control4.

Automatic preparation requires an **admin HA access token** in the Coordinator.
A permission error does not mean HA is disconnected. Native direct control
without the companion keeps its existing permission requirements.

An activated device keeps one event entity across restarts and can be reused
by several consumers. No separate event entity selection is required. Devices
that are never selected have no generated controls. Scene/button reporting must
already be enabled in the device's own integration or manufacturer settings.

## Optional customization and advanced setup

Use **Configure** on an activated control to edit its name, button assignments,
linked HA light or unusual LED mappings. Known Zooz paddle layouts use verified
Central Scene identities; unknown multi-button inputs keep their individual
identities until you assign roles. Source ownership is preserved while metadata
is temporarily unavailable. Saving an explicit reconfiguration can adopt a
replacement source; a restart cannot do so silently.

An explicitly linked HA light takes precedence over the physical relay and
retains that choice across preparation/restarts. If the target is missing or
unavailable, load commands are blocked. Clear the link explicitly to return to
the selected physical endpoint. A bound C4 light remains controlled through
Composer Controller Links and does not command the HA relay.

**Add control** remains available for HA-only or advanced manual setup: choose
a device, review the detected inputs, assign optional paddle roles, and save.
Supported LEDs are automatic; Customize LED mappings is an exceptional override.
Delete an activated control explicitly through HA if it is no longer used;
disconnecting a C4 driver does not remove entities referenced by automations.

## Check the gestures

Press each button and try a double tap. In **Tools → States**, find the new gesture entity. Its event timestamp should update, and attributes such as `button_id`, `event_type` and `press_count` identify the action. A double tap reports `press_count: 2` when the device supports it.

## Use the gestures

**Home Assistant:** use the new event entity in an automation. The [automation examples](docs/automations.md) show how to act on fresh gestures without replaying startup history.

**Control4:** select the gesture control using **Actions → Select Device** in the compatible HA Switch or HA Light driver, then choose its Operation Mode:

- **Programming Events Only** for Composer button-event programming.
- **Control Connected C4 Light** to operate an existing C4 light through HA Light’s Top/Bottom/Toggle **Controller Links**.
- **Control HA Device with Button Events** for gestures plus an explicitly linked HA load.

For smart bulbs or a separate C4 light, configure the wall switch’s smart-bulb or detached mode according to its manufacturer’s instructions.

## Optional indicator LEDs

Status indicators are discovered automatically through the standard Z-Wave Indicator command class and supported device profiles, including Zooz LED configuration controls. The review screen shows what is supported. On/off, brightness and colors are exposed only when the device and its discovery path provide them; an identification blink alone does not provide a status LED. No LED commands run during setup or restart.

In the C4 driver, **Properties → LED Behavior** controls the indicator independently of color. **Use Device Setting** is the default. HA Light also supports **Follow Bound Device** for a connected C4 light. Setting a color preserves the device’s LED mode.

Automatic profiles include Zooz ZEN26/27/30/32/71/72/76/77/78 and the listed GE/Jasco models. Multi-button controls export each input separately, and compatible C4 LED commands can select every indicator. See the [supported device table and LED setup guide](docs/leds.md).

For a device outside automatic support, **Customize LED mappings (advanced)** provides an explicit override.

## Updates and troubleshooting

Install Marketplace updates and restart HA. Existing controls normally keep their entity IDs and assignments.

- **Integration missing:** restart HA, refresh the browser and search under Add integration. For a manual install, check the folder path above.
- **No buttons detected:** check the physical device’s HA integration and enable its event entities or scene reporting. Automatic discovery needs event entities or supported physical device triggers. [Source support and advanced configuration](docs/sources.md) covers exceptions.
- **C4 does not respond:** check the selected gesture entity, Operation Mode, Coordinator connection and Controller Link bindings.

Based on [DeviceSide/DeviceSide](https://github.com/DeviceSide/DeviceSide) by jasonmx and its contributors. See [credits](UPSTREAM.md) and [LICENSE](LICENSE).
