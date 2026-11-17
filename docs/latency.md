# Gesture latency diagnostics

HA Gesture Bridge 0.1.2 adds an optional acknowledgment action for receivers.
Normal event publication and load behavior do not wait for acknowledgments, retry
missing ones, or depend on them. Older receivers ignore the additive capability
flag; receivers must leave acknowledgment traffic disabled during normal use.

HA Light build `communication-latency-1` can acknowledge each newly accepted live
gesture with **Latency Diagnostics = On**, independently of Debug Mode. Leave
**Debug Mode = Off** to avoid verbose logging overhead. Acknowledgment happens
after processing/forwarding the gesture.
The gesture event advertises the integer `bridge_acknowledgment_version: 2`.
Receivers must require this exact version before using the generic `receiver_*`
fields. The earlier boolean capability is no longer advertised, so its receivers
do not call an action with an incompatible schema. Initial state,
snapshots, duplicates and rejected events are not latency samples.

## What is measured

The interval starts immediately before HA publishes a fresh aggregate gesture and
ends when its matching acknowledgment reaches HA. Both endpoints use HA's
monotonic clock. The result is a **publish-to-acknowledgment round trip**, including
outbound delivery, receiver processing/forwarding and return delivery. It does not
require HA and C4 UTC clocks to agree.

This round trip bounds the forward delay; dividing it by two does **not** prove
the one-way latency because transport and scheduling can be asymmetric. It does
not measure the physical click before the switch reports its classified gesture,
nor when a physical light visibly responds after a command.

The sample also retains HA publish/ack receipt UTC timestamps and the C4 receive,
processed and optional forwarded UTC epoch-millisecond values. Those timestamps
help correlate logs; subtracting timestamps from different machines assumes their
clocks agree. The separate `receiver_processing_ms` value is a receiver-reported local
duration. A discrepancy with its receiver UTC timestamp interval is flagged as
`receiver_clock_discontinuity`, while the HA-clock round trip remains useful.

## Read results

Use **Download diagnostics** on the HA Gesture Bridge integration card. Recent
samples appear under `latency.latest_latency_samples`; HA's integration log also
records an INFO message for each valid acknowledgment. No acknowledgment writes
another gesture state or publishes a new event.

Diagnostics retain at most 32 samples. Pending publications are bounded to 64,
expire after 30 seconds, and accept at most eight different receiver identities.
Unknown/stale sessions, sequences, unloaded controls and duplicate acknowledgments
from one receiver are rejected. Reloading/removing a control clears its pending
publications. A missing acknowledgment is not proof that the gesture was lost.

Downloaded samples replace control IDs with anonymous `control_index` values and
omit entity, receiver and session IDs, names, source payloads and credentials.
Sequence numbers and timestamps remain for local correlation and reveal event
timing; review those before sharing diagnostic downloads.

## Receiver action contract

Call `ha_gesture_bridge.acknowledge` with these **service_data** fields (not a
separate service target). The entity ID can change; the current bridge session
and gesture sequence must identify an actual recent publication.

```yaml
entity_id: event.office_switch_gestures
bridge_session_id: 0123456789abcdef0123456789abcdef
gesture_sequence: 14
receiver: control4:123
receiver_received_epoch_ms: 1794729172042
receiver_processed_epoch_ms: 1794729172048
receiver_forwarded_epoch_ms: 1794729172047
receiver_processing_ms: 6
```

`receiver_forwarded_epoch_ms` is optional and must be omitted when no connected-light
command was sent, such as a double-tap used only for programming. UTC values are
finite non-negative milliseconds; processing duration is limited to 60 seconds.
No clock-dependent diagnostic value grants gesture validity or load capabilities.
The action is diagnostic only and has no load-command or execution backend.
