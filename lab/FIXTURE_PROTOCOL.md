# Fixture controller protocol

The controller uses newline-delimited UTF-8 JSON at 115200 baud. Every request
receives exactly one response. A response must contain `"ok": true`; otherwise
the HIL run fails closed.

Requests:

- `{"command":"identify"}` returns the immutable rig `hardware_id` and the
  64-lowercase-hex `firmware_sha256`; both must match the signed safety gate.
- `{"command":"set_mode","value":"setup"}` drives the existing RUN/SETUP
  input through an isolated open-drain output.
- `{"command":"set_mode","value":"run"}` selects a normal cold cycle.
- `{"command":"set_power","value":true}` enables the isolated switched 5 V
  path and returns the fixture's numeric monotonic `edge_us` timestamp for the
  actual power-on edge. `false` disables it. The host records its own monotonic
  receipt time for the same response so fixture elapsed time can be mapped into
  the controller clock domain.
- `{"command":"wait_done","timeout_ms":180000}` waits for and timestamps the
  BCM26/TPL5110 DONE edge. The response includes numeric monotonic `edge_us` and
  `pulse_us` values. The pulse must be 10,000–1,000,000 microseconds. The
  controller correlates its host receive time with the new durable target run
  ID and cycle timestamps; the GPIO fixture is not expected to know that run ID.
  The release gate subtracts the power-on `edge_us` from this DONE `edge_us` to
  measure true cold wake-to-DONE; application process timing remains diagnostic.
  The Pi 3 independently polls `iw dev wlan0 station get <zero-mac>` from before
  power-on until the fixture-mapped DONE edge to prove that association really
  occurred. The conservative power-on-to-DONE interval is the promotion
  `wifi_power_budget_seconds`; association integration and the inner runtime
  timer remain diagnostic.
  Firmware must latch the first DONE edge after each power-on, so a host may
  prove USB SSH reachability before issuing `wait_done` without losing the edge.
- `{"command":"wait_phase_cut","phase":"persist","timeout_ms":180000}`
  waits for a receive-only UART phase event, removes switched power at that
  boundary, and returns the phase and monotonic `edge_us`.
- `{"command":"wait_update_cut","phase":"verify","timeout_ms":180000}`
  accepts only `begin`, `stage`, `verify`, or `activate`, waits for exactly one
  schema-1 `update_phase_boundary` UART event with that phase, removes switched
  power, and returns the phase and monotonic `edge_us`. This command is distinct
  from lifecycle cuts so one campaign cannot be mistaken for the other.
- `{"command":"measure_rails"}` samples the powered-off target and returns
  numeric `zero_5v_v` and `zero_3v3_v`. Both must remain below 0.1 V or the HIL
  run stops before power is enabled.

The fixture firmware must default to power off and SETUP on reset, use a
watchdog, reject unknown fields or commands, capture application phase events
from the isolated receive-only UART, and never drive that UART line toward the
Zero. Fixture firmware is outside this repository; its version and SHA-256
belong in every physical HIL report.

The Zero uses the PL011 on GPIO14 TX at 115200 baud. Kernel/early-boot console
text and schema-1 application JSON lines share this receive-only stream; serial
getty/login is masked. The parser must ignore every non-JSON line and every JSON
object whose `schema_version`/`event` fields do not identify a TideTracker
runtime phase, update boundary, or target identity event. It must never transmit
toward GPIO15 RX.

The controller independently records the same receive-only stream. A UART read
error, an empty capture, an oversized capture, a missing requested phase, or
more than one requested update boundary fails the HIL run. Each captured log is
bound into the result with its byte count and SHA-256. A final USB-disconnected
field boot must also emit exactly one schema-1 `target_identity` record carrying
`release_id`, `execution_source`, and the deterministic `image_build_id`; these
must match the externally signed image/release attestation in the safety gate.
