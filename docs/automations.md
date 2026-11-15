# Use gestures in HA automations

These examples use the aggregate `event.office_scene_control`. Replace that ID and the targets with your own. They do not require Control4. The guard accepts only a fresh current-session occurrence; load-only snapshots and restored history do not execute an action. The tests load both examples into HA's real automation engine, replacing only the target service boundary.

## Double tap

```yaml
automation:
  - alias: Office top double tap
    triggers:
      - trigger: state
        entity_id: event.office_scene_control
    conditions:
      - condition: template
        value_template: >-
          {{ trigger.to_state is not none and trigger.from_state is not none
             and trigger.to_state.state != trigger.from_state.state
             and trigger.to_state.attributes.get('bridge_update_kind') == 'gesture'
             and trigger.to_state.attributes.get('gesture_session_id') == trigger.to_state.attributes.get('bridge_session_id')
             and trigger.to_state.attributes.get('button_id') == 'top'
             and trigger.to_state.attributes.get('event_type') == 'press'
             and trigger.to_state.attributes.get('press_count') == 2 }}
    actions:
      - action: scene.turn_on
        target:
          entity_id: scene.office_evening
```

Do not trigger solely on the `event_type` attribute changing: consecutive identical presses would share that value.

## Optional DeviceSide smooth dimming

The complete [DeviceSide hold example](../examples/deviceside-hold.yaml) uses the same state-change trigger and freshness/session guard, then chooses by the reported button and kind. Targets must already be supported by DeviceSide. This bridge supplies input events; DeviceSide supplies execution.

```yaml
actions:
  - choose:
      - conditions: "{{ trigger.to_state.attributes.get('event_type') == 'hold_start' and trigger.to_state.attributes.get('button_id') == 'top' }}"
        sequence:
          - action: deviceside.raise_brightness
            target: {entity_id: light.office}
      - conditions: "{{ trigger.to_state.attributes.get('event_type') == 'hold_start' and trigger.to_state.attributes.get('button_id') == 'bottom' }}"
        sequence:
          - action: deviceside.lower_brightness
            target: {entity_id: light.office}
      - conditions: "{{ trigger.to_state.attributes.get('event_type') == 'release' }}"
        sequence:
          - action: deviceside.stop
            target: {entity_id: light.office}
```

Configure this only for a source that reports release. Hold repeats do not restart the move. A missing release or outage does not become a fabricated physical release; use a separate source-availability stop policy if the selected execution backend needs one. DeviceSide compatibility and experimental settings still apply.

## Control4 programming

Select the aggregate on the HA Switch/Dimmer consumer. Its reported double/triple gestures can trigger Composer scene programming. In C4 Button Controller mode, singles/holds/releases instead forward through existing Button Link connections to the selected C4 light. Keep the target light's driver responsible for its actual load. Native discovery and manual/direct control do not require this HA companion.
