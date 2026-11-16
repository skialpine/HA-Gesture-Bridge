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

## Configure a device

Open **HA Gesture Bridge → Add control**, then:

1. **Choose your device** from the list and select **Next**. Supported status indicators are discovered automatically. Leave **Customize LED mappings (advanced)** unchecked for normal setup.
2. **Review the detected buttons, gestures and status indicators.** The integration finds the event entities or physical button triggers HA already exposes. It does not infer gestures from the light turning on or off.
3. **Check the suggested control name.** For example, **Office Switch → Office Switch Gestures**. You can change it.
4. **Check the button assignments.** Top, Bottom and Toggle connect the detected buttons to familiar C4 paddle events. Suggested assignments use the device’s button names; other buttons keep their original names.
5. Leave **Linked switch or light** empty for gesture programming or a light connected through C4. Select a linked HA load only when your consumer needs its state and capabilities; linking it does not operate it automatically.
6. Select **Save**.

The integration creates an entity such as `event.office_switch_gestures`. Use the actual entity ID shown by HA. Add the integration once, then use **Add control** for each additional device.

There is no source-type menu or JSON mapping editor in normal setup. To change a device or its assignments, edit the control and follow the same device-and-review workflow.

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
