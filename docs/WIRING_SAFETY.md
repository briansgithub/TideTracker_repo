# Direct Pi Zero lab wiring and safety gate

The USB management link is safe only when it cannot power the Zero around the
TPL5110. Raspberry Pi Zero USB gadget mode normally receives both data and 5 V
through the OTG connector, so an ordinary data cable is **not** acceptable for
this rig.

## Required topology

- Connect the Pi 3 to its normal Ethernet LAN and a separately powered USB hub.
- Connect the hub to the host side of a self-powered, full-speed/high-speed USB
  2.0 data isolator.
- Power the isolator's target side from the Zero's TPL5110-switched 5 V rail.
  The host-side VBUS must not reach the target side.
- Connect the target USB data pair to the Pi Zero W micro-USB connector nearest
  HDMI (`USB`), never to `PWR IN`.
- Use a fixture-controlled, normally-off load switch rated for at least 5 V,
  2 A. The switch controls the same rail used in field operation.
- Drive the existing RUN/SETUP input through an isolated open-drain output.
- Sense DONE/BUSY through high-impedance isolated inputs.
- Feed the switched 5 V and 3.3 V rails to isolated, calibrated fixture ADC
  channels so automation can prove the powered-off voltage before every run.
- Connect the Zero's BCM14/PL011 TX to the capture adapter's RX through a 3.3 V-safe,
  receive-only isolated path. Leave the adapter TX physically disconnected.
- Aim a fixed UVC camera squarely at the complete display and lock its focus,
  exposure, white balance, mount, and lighting.

## Mandatory human gate

With every device unpowered, check continuity and polarity against the actual
wiring diagram. Then perform these measurements before automation is enabled:

1. Fixture power off, USB connected: Zero 5 V to ground is below 0.1 V.
2. Fixture power off, UART and GPIO sensing connected: Zero 3.3 V is below
   0.1 V. Any measurable powering is a stop condition.
3. Fixture power on: the switched rail is 4.75–5.25 V at the Zero under load.
4. Fixture power off: SSH at `10.77.0.2` disappears within 15 seconds.
5. Verify the fixture defaults to power off and SETUP after its own reset.
6. Verify the physical RUN/SETUP switch still overrides or disconnects the
   fixture as documented for field use.

Record the exact model and serial/asset ID for the isolator, switched-power
fixture/load switch, UART adapter, camera, display, TPL5110/power timer, Pi Zero,
and SD card in both the private controller config and the signed gate. Do not
automate power until a person has checked those labels and signed this gate.

Copy `lab/safety_gate.example.json` outside Git, enter the measurements and rig
identity, and set `approved` plus each topology boolean only after this review.
Sign the exact JSON bytes with a dedicated Ed25519 lab-approval key and save the
raw signature as `<gate-name>.json.sig`. The HIL controller verifies this
signature against the controller-image-pinned public key before opening fixture
serial. The gate is also bound to a fresh nonce, seven-day-or-shorter expiry,
fixture firmware hash, rig ID, and target `known_hosts` hash. Its live
`identify` and `measure_rails` commands
must independently keep both off rails below 0.1 V throughout the campaign.
The controller rechecks the expiry before every new fixture, target, Wi-Fi,
portal, UART, camera, and field-probe action; reaching it stops the campaign and
promotion rejects evidence finished after it. Safety teardown remains permitted
to remove power, restore the controller AP, stop captures, and verify off rails.
The same signature covers the canonical camera capture, camera validator, and
USB-absence probe argv plus hashes of their resolved executables and every
declared script/config/reference file. Any changed identity or file stops the
run before fixture serial opens; each evidence record repeats the signed tool
identity for offline promotion checks.

The legacy TideTracker uses a 500 ms LOW settle followed by a 500 ms HIGH DONE
pulse. Both normal and failsafe paths preserve that timing until isolated
fixture capture establishes a safe shorter margin on this exact TPL5110/board.

Software provides an at-most-one-attempt contract per boot, not a proof that an
edge occurred exactly once. Before invoking GPIO, the sole DONE owner records a
private, fsynced intent marker in `/run`; completion is recorded only after the
selected backend confirms LOW/HIGH/LOW. An intent without completion is
ambiguous and neither the application nor systemd may retry it. Retrying could
duplicate a rising edge, while marking completion before GPIO could suppress the
only edge. No software-only ordering can eliminate both crash windows.

Release approval therefore requires isolated fixture capture while the DONE
owner is killed at each boundary: before intent, after intent but before LOW,
immediately before and after HIGH, before final LOW, after final LOW but before
completion recording, at the outer command timeout, and during systemd stop.
Each case must prove at most one rising edge, bounded termination, and recovery
to a cold boot; cases killed before HIGH may rely on the external timer/watchdog
but must never provoke a software retry.

## Recovery boundary

The rig can recover application failures and bad release bundles. It cannot
recover a non-booting card, damaged boot filesystem, unsafe wiring, or failed
fixture without hands-on service because v1 deliberately has no SD-card mux.
