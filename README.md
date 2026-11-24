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

For the current Home Assistant Light and Switch suite, open **Device Setup**
after configuring the Home Assistant Connection. Choose the physical switch,
dimmer or remote, then select **Use selected device**. HA prepares its gestures
and supported LEDs on first use. Browsing creates no gesture
entities, attaches no input listeners and changes no device parameters.
Ordinary loads without physical gestures use their existing entity directly.

The read-only `ha_gesture_bridge.get_devices` action supplies a bounded,
versioned device list to the Coordinator. Subsequent openings can send its
revision to receive an unchanged confirmation instead of the list. It includes
native Home Assistant light and switch groups. The helper excludes hidden and
disabled endpoints and devices exported from Control4. The helper is optional
for native light and switch setup. See the [picker contract](docs/catalog.md#picker-service)
for response details.

Use an administrator’s Home Assistant token in the current suite’s Home
Assistant Connection. The driver uses it for device feedback and automatic
preparation. If Home Assistant is connected and setup reports a permission
error, check the account used to create the token.

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
the selected physical endpoint. A connected Control4 light uses the light
selected by its Control Link in Composer.

**Add control** remains available for HA-only or advanced manual setup: choose
a device, review the detected inputs, assign optional paddle roles, and save.
Supported LEDs are automatic; Customize LED mappings is an exceptional override.
Delete an activated control explicitly through HA if it is no longer used;
disconnecting a C4 driver does not remove entities referenced by automations.

## Check the gestures

Press each button and try a double tap. In **Tools → States**, find the new gesture entity. Its event timestamp should update, and attributes such as `button_id`, `event_type` and `press_count` identify the action. A double tap reports `press_count: 2` when the device supports it.

## Use the gestures

**Home Assistant:** use the new event entity in an automation. The [automation examples](docs/automations.md) show how to act on fresh gestures without replaying startup history.

**Control4:** choose the device in **Device Setup**, then select the Light or
Switch in Composer **Programming**. Choose the button and gesture the device
reports, including up to three taps. If events are missing, refresh the
**Device Events** list and reselect the driver. Look for a label such as
**Top - 2 taps** on a Switch. Reconnect Composer to Director if the events are still stale.
**Button behavior** sets how the selected wall control is used:

- **Automatic** follows the device and its existing connections.
- **Buttons only** keeps button presses available for programming with app control off.
- On a Light, **Connected Control4 light** operates existing Control4 lighting through **Top Control Link**, **Bottom Control Link** or **Toggle Control Link**.
- On a Switch with a linked device and buttons, **Control device and report presses** uses the linked load and makes gestures available to Programming.

For an older prototype Control4 driver, use the setup instructions included
with that driver to find its controls and programming labels.

For smart bulbs or a separate C4 light, configure the wall switch’s smart-bulb or detached mode according to its manufacturer’s instructions.

## Optional indicator LEDs

Status indicators are discovered automatically through the standard Z-Wave Indicator command class and supported device profiles, including Zooz LED configuration controls. The review screen shows what is supported. On/off, brightness and colors are exposed only when the device and its discovery path provide them; an identification blink alone does not provide a status LED. No LED commands run during setup or restart.

In the Control4 driver's **Device Setup**, look for **Indicator lights on**
followed by the device's name. Color, behavior and brightness dropdowns apply
when you choose an option. For a brightness number field, enter a percentage
and select **Apply**. The Light can also follow a connected Control4 light.
Setting a color preserves the device's LED mode.

Automatic profiles include Zooz ZEN26/27/30/32/71/72/76/77/78 and the listed GE/Jasco models. Multi-button controls export each input separately, and compatible C4 LED commands can select every indicator. See the [supported device table and LED setup guide](docs/leds.md).

For a device outside automatic support, **Customize LED mappings (advanced)** provides an explicit override.

## Updates and troubleshooting

Install Marketplace updates and restart HA. Existing controls normally keep their entity IDs and assignments.

- **Integration missing:** restart HA, refresh the browser and search under Add integration. For a manual install, check the folder path above.
- **No buttons detected:** check the physical device’s HA integration and enable its event entities or scene reporting. Automatic discovery needs event entities or supported physical device triggers. [Source support and advanced configuration](docs/sources.md) covers exceptions.
- **Control4 does not respond:** check the selected device and **Controls** in
  **Device Setup**. If the driver reports a disconnected Home Assistant server,
  check the **Home Assistant Connection** in Composer. For a Control4 light,
  check the corresponding **Control Link**.

Based on [DeviceSide/DeviceSide](https://github.com/DeviceSide/DeviceSide) by jasonmx and its contributors. See [credits](UPSTREAM.md) and [LICENSE](LICENSE).
