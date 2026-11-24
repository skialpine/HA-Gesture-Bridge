# Status LEDs

HA Gesture Bridge discovers supported status LED controls when you choose a physical device. Compatible consumers, including the Control4 HA Switch and Dimmer drivers, see the available indicators and their capabilities automatically.

## Set an indicator from Control4

1. In the Light or Switch driver's **Device Setup**, choose the wall switch and
   select **Use selected device**.
2. Find **Indicator lights on** followed by the switch name. If the channel
   offers **Indicator color**, choose a color such as Blue. Color, behavior and
   brightness dropdowns apply on selection. For a brightness number field,
   enter a percentage and select **Apply**.
3. Wait for the setting to be reported, then reopen Device Setup and check the
   saved value. Check the physical indicator separately on the wall switch. If
   it follows the load, check it when the load is in the appropriate state.

For Home Assistant–only or advanced setup, add or edit a control in HA Gesture
Bridge and select its physical device. Leave **Customize LED mappings (advanced)**
unchecked to use the detected settings. Review **Status LEDs**, then save.

You do not need to copy entity IDs or write JSON for supported devices. Discovery reads the device's capabilities and never changes a relay, sends an LED command or changes LED behavior during setup, startup, reconnect or a snapshot.

The integration uses two discovery paths:

- **Standard Z-Wave Indicator command class:** on/off and brightness where the device advertises writable status indicators. Identification-only blink controls, sound indicators and timer properties are not exported as status LEDs. The standard does not provide general RGB color control.
- **Supported device profiles:** documented manufacturer-specific LED configuration parameters, including supported Zooz devices. A profile checks the selected device and its actual parameter metadata before exposing color, brightness or power. An unknown device or firmware does not acquire capabilities from a similar name.

An **Identify** button in HA is not evidence that a device provides an independently controllable status LED through the standard interface. A switch can support identification and expose its normal indicator only through a manufacturer profile. Unsupported devices still export their gestures normally.

Each discovered channel represents one controllable indicator. A shared indicator is not duplicated into fictional Top and Bottom LEDs. Discovered channel IDs remain stable across labels and HA entity renames. Reported state comes from device feedback; unknown values do not become a confirmed on/off state.

## Supported device profiles

| Devices | Indicators | LED controls |
| --- | --- | --- |
| Zooz ZEN71, ZEN72, ZEN76, ZEN77 | One shared status LED | On/off, four colors, three brightness levels |
| Zooz ZEN26, ZEN27, ZEN78 | One fixed-color status LED | On/off |
| Zooz ZEN30 | Dimmer and relay indicators | Independent on/off, color and brightness |
| Zooz ZEN32 | Main switch and four small-button indicators | Independent on/off, color and brightness; reported firmware options select the palette and levels |
| GE/Enbrighten 46201 / ZW4008; Jasco 46562 / ZW4008 | One fixed-color status LED | On/off |
| Leviton DZ15S, DZ6HD | One fixed-color locator LED | On when the load is on, on when the load is off, or always off |

Profiles validate the actual hardware identity and the complete parameter option tables. Older or different firmware that does not expose the reviewed controls remains unsupported. Standard Indicator discovery can cover additional devices without a manufacturer profile.

A fixed-color LED uses the behavior commands the device supports. If it supports forced on or off, its metadata uses logical white or black to report that setting. Brightness commands require a device that exposes adjustable LED brightness.

Multi-button devices keep all detected button inputs. Top, Bottom and Toggle assignments are optional; additional buttons retain independent named programming events. The Control4 drivers list indicator channels by name. A Light's **Top Control Link**, **Bottom Control Link** and **Toggle Control Link** connect the corresponding paddles to existing Control4 lights.

## LED behavior

In the current Control4 Light or Switch suite, open **Device Setup** and find
**Indicator lights on** followed by your device’s name.
Use **Behavior** for an individual channel or **Indicator behavior** when the
driver shows a shared setting. Dropdowns apply on selection. Reopening Device
Setup shows the setting reported by Home Assistant.

The device and its Control4 connections determine which choices are available.

| Choice | Result |
| --- | --- |
| Keep current LED setting | End Control4 tracking and leave the device in its current LED mode. |
| On when the device is on | The LED lights when the wall device's own load is on. |
| On when the device is off | The LED lights when the wall device's own load is off. |
| Always on | Keep the indicator on and preserve its saved color. |
| Always off | Keep the indicator off and preserve its saved color. |
| Follow the linked light | The Light driver follows the connected Control4 light. |

For **Follow the linked light**, use **Follow the light linked to** to choose the
paddle connected to the Control4 light you want to track. The driver saves
those two choices as you select them. Use **When the light is on** and
**When the light is off** to choose tracking colors. Black in a saved tracking
color turns the indicator off. The indicator follows feedback from the chosen
light.

**Set LED Current Color** now changes color only. On a ZEN76, that changes parameter 14 while preserving parameter 2. Brightness changes only parameter 15. To turn the LED off, select **Always Off** or use the **Set LED Behavior** programming command. Black is not a writable color option on these Zooz devices. Fixed-color indicators use behavior commands rather than color commands.

The per-channel **Set LED Behavior** programming command applies one explicit device setting. If **Follow Bound Device** remains selected, later connected-light feedback can override that one-time setting.

The HA `ha_gesture_bridge.set_led` action also accepts `behavior` independently of color and brightness. Supported values are `follow_load`, `follow_load_inverted`, `always_on` and `always_off`; check the channel's `behaviors` metadata. The action can set multiple fields in one explicit request.

## Custom mappings (advanced)

Use this only when automatic discovery does not cover the device's LED controls, or when you need a different explicit mapping. Custom mappings override automatic LED discovery for that control.

1. Enable the device's LED configuration entities in Home Assistant if they are disabled. Under **Settings → Devices & services**, open the device and review its configuration parameters. Check the device's documentation for parameter meaning.
2. Open each relevant `select` or `number` entity in **Tools → States**. Copy its actual entity ID and exact option strings or numeric range/step. Do not copy a light, switch or relay entity.
3. Edit the Gesture Bridge control and select **Customize LED mappings (advanced)**. Review the buttons, select **Next**, then enter the LED mappings as a JSON list and select **Save**. Enter `[]` to remove the override and return to automatic discovery. Edits of the same device keep its custom mappings; changing devices removes the previous device's mappings.
4. Retain each LED's `id` and logical `button_id` when changing its label. Use one channel per physical LED. Select/number endpoints cannot be reused by multiple channels in one control.

For a Zooz model or firmware outside the supported profiles, copy its actual options rather than guessing color codes. A single shared indicator can be mapped to `toggle`. Use `top` or `bottom` only for an independently controllable indicator associated with that paddle. A monochrome on/off indicator needs only a `power` mapping.

This generic example requires three separate configuration entities. Replace all IDs and options with values from your installation:

```json
[
  {
    "id": "status",
    "label": "Status LED",
    "button_id": "toggle",
    "color": {
      "entity_id": "select.paddle_led_color",
      "values": {"FFFFFF": "White", "0000FF": "Blue", "FF0000": "Red"}
    },
    "power": {
      "entity_id": "select.paddle_led_mode",
      "on": "Always on",
      "off": "Always off"
    },
    "brightness": {
      "entity_id": "number.paddle_led_brightness_parameter",
      "values": {"100": 0, "60": 1, "30": 2}
    }
  }
]
```

The brightness example maps percentage labels to parameter values; it does not assume parameter value `100` means full brightness. All three roles accept either `select` options (strings) or `number` values (finite numbers). For example, brightness can use `{"100":"Full","60":"Medium","30":"Low"}` on a select entity. A number endpoint may map color hex keys to numeric color codes or power `on`/`off` to numeric modes. Use only documented LED configuration parameters.

Color and power are individually optional, but at least one is required. Without a color mapping, the channel is fixed-color; the metadata uses logical white to report on without promising a physical white LED. Without an explicit power mapping, black/off is unsupported. Each channel allows at most 64 nonblack colors, integer brightness keys from `0` through `100`, and distinct backend values. An installation can configure up to 16 channels per control. Labels accept at most 128 characters without control characters. Unknown fields, ambiguous mappings and load entities are rejected.

UI saves capture endpoint entity-registry identities. Renaming an endpoint follows that identity; deleting it never silently attaches a different device that reuses its old entity ID. YAML can also specify `registry_id` inside an endpoint to pin its identity across restarts. After replacing hardware, explicitly update the mapping rather than retaining a deleted endpoint's registry ID.

## Commands and feedback

Call `ha_gesture_bridge.set_led` with the aggregate event entity and the channel ID reported in its `bridge_leds` attribute. This example uses the custom `status` channel above:

```yaml
action: ha_gesture_bridge.set_led
data:
  entity_id: event.office_switch_gestures
  led_id: status
  color: "0000FF"
  brightness: 60
```

Provide `color`, `brightness`, `behavior`, or an explicit combination. Colors are six hexadecimal digits without `#`; case is normalized. Color changes preserve behavior, and behavior changes preserve the stored color. Black requires explicit `behavior: always_off`; it does not write a color parameter. Brightness chooses the nearest supported percentage, with ties going to the lower percentage, while preserving behavior. A standard intensity-only indicator rejects a power transition unless behavior is explicit.

The service validates the entire action, including current endpoint availability, advertised select options and number ranges/steps, before any write. It then awaits the required endpoint calls in order. Concurrent actions for one control are serialized, and unloading or reconfiguring the control stops any remaining writes. A later hardware/service failure can still leave an earlier write applied; HA cannot make multiple device parameter writes transactional.

For custom mappings, reported color and brightness come only from confirmed endpoint state. There is no optimistic feedback. An unknown/unavailable/restored endpoint, missing configured option or invalid numeric range makes the channel unavailable. A valid current option outside the configured mapping leaves its color or brightness unknown until a mapped value is confirmed. A known power-off option reports black regardless of the saved color.

Confirmed changes publish aggregate **snapshots**. They never create a gesture, increment the gesture sequence, acknowledge a click, or send another LED command. The advertised metadata contains channel IDs, labels, supported palette, availability and confirmed state; it contains no backend entity IDs or option names. Downloaded diagnostics also exclude LED backend identities and configuration.

When Home Assistant confirms a writable color setting, `configured_color` reports the saved color even if the LED is off or follows the switch. `color` describes the reported output separately. Missing readback stays unknown.

Verified profiles and custom mappings publish their discrete brightness choices as `brightness_levels`. Device Setup shows those choices alongside the confirmed current brightness. Other supported brightness controls retain the percentage input.

See the [wire contract](gesture-contract.md#optional-status-leds) for receiver integration. These LEDs are distinct from brightness of the controlled room light. Keep load/dimming behavior in DeviceSide, your explicit HA automations, or the connected Control4 light driver.
