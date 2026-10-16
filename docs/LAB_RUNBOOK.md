# Pi Zero W direct-execution lab runbook

## Safety and control plane

The workstation edits/tests, the Ethernet-managed Pi 3 controls the fixture/AP/
camera, and the Zero executes production code. USB Ethernet is static
`10.77.0.1/30` ↔ `10.77.0.2/30` management only. The Zero nftables policy permits
only established SSH/SFTP responses to the controller peer, drops all other
USB output/input/forwarding, and is a required dependency of production
services. NetworkManager supplies no USB default route, gateway, or DNS.

Dry-run and `--simulate` do not open SSH, serial, camera, network controls, or
power. Before every real command, stop for approval of the named hardware/card
operation. Real HIL then requires:

- `--execute --execute-confirmation EXECUTE-TIDETRACKER-HIL`;
- explicit `--expected-release-id` and `--expected-execution-source` (`built-in`
  or `verified-release`);
- a fresh nonce-bound, <=7-day signed safety JSON and `.sig`, verified only by
  the immutable controller key `/etc/tidetracker/lab-safety-approver.pub.pem`.
- signed `approved_operations` scope for every requested campaign and, for
  updater cuts, every requested `approved_update_power_cut_phases` boundary;
- exact signed image SHA-256, role-manifest SHA-256, deterministic image build
  ID, canonical five-key policy digest, and Pi 3 controller machine-ID SHA-256
  matching the private controller
  configuration. The orchestrator hashes one bounded, regular, root-owned
  `/etc/machine-id` snapshot and binds the verified value into every session;
- an exact model plus serial/asset ID for the USB data isolator, switched-power
  fixture, UART adapter, camera, display, TPL5110/power timer, target Zero, and
  target SD card. All eight identities must match the private controller config
  and are part of the cross-session promotion environment identity;
- signed identities for the camera capture, camera validator, and field USB
  probe: canonical absolute argv, resolved executable size/SHA-256, and the
  size/SHA-256 of every explicitly listed support/config/reference file.

All camera tools, the dedicated controller-to-Zero SSH key/known-hosts, fixture/UART ports, controller NM
profiles, Wi-Fi tests, field USB probe, signed gate, and electrical identity are
preflighted before fixture serial opens. A controller-wide nonblocking lock is
held from before serial open through final power-off/close, so a second agent
cannot race the rig. The lock records the session path and gate hash.

The ordinary `tidetracker-hil` wrapper remains unprivileged for plans and
simulation. An exact `--execute` request is routed through the installed
root-only launcher, which accepts only the real `tidetracker` account and then
clears the inherited environment. Target access pins `/usr/bin/ssh` and
`/usr/bin/sftp`, ignores every SSH configuration file and agent, permits only
public-key authentication, and accepts exactly one `10.77.0.2 ssh-ed25519`
record in the dedicated root-owned known-hosts file. The private key must derive
the controller-to-Zero public identity recorded in the installed five-key
policy before the fixture can be opened.

Real artifacts may be written only as a new direct child of
`/var/lib/tidetracker/controller/hil-results`. The orchestrator verifies that
this root is a non-symlink, root:`tidetracker` mode-`0770` directory on the
controller's writable ext4 `TIDEDATA` UUID. Simulation may use a temporary
workstation directory. Gate, signature, and approval key are each opened once,
snapshotted privately, and the same bytes are verified, parsed, and hashed with
the fixed `/usr/bin/openssl`.

## Bring-up

1. Preserve and label the old Pi 3 and Bullseye Zero cards.
2. Create cards from the exact signed artifacts in `docs/IMAGE_RELEASE.md`.
3. Complete `docs/WIRING_SAFETY.md`; capture real readings and sign a copy of
   `lab/safety_gate.example.json` outside Git.
4. Before either role build, run the guarded `deploy/prepare-keystore.ps1`
   workflow in `docs/IMAGE_RELEASE.md`. It atomically creates the two distinct
   SSH identities plus the application, lab, and image signing identities under
   `%LOCALAPPDATA%\TideTracker\keystore\v1`; both builds receive all five public
   halves and never receive the `private/` tree. Record the public policy digest
   and retain the public-only keystore record. After controller first boot,
   stop for explicit controller-mutation approval before installing only
   `private/controller-to-zero-ssh-ed25519` at
   `/var/lib/tidetracker/controller/ssh/controller-to-zero-ed25519`, root-owned
   mode `0600`; the HIL command uses a constrained sudo launcher because the
   unprivileged `tidetracker` account cannot read it. The workstation-to-controller
   private half stays on the workstation and only its public half is authorized
   by the controller. After the Zero's first boot, verify its host-key fingerprint by
   the isolated UART channel before storing it in the root-owned mode-`0600`
   `/var/lib/tidetracker/controller/ssh/zero-known-hosts`; never use
   `StrictHostKeyChecking=no`. This SSH automation key is the only private key
   intentionally present on either Pi and is not a release or approval signer.
5. Copy `lab/hil_config.example.json` outside Git. Its pinned SSH paths name the
   controller-to-Zero files above; the workstation-to-controller private
   identity must not appear in this configuration. Set fixture/UART ports, SSH
   paths, camera capture/comparator, all 12 portal profiles, intentionally bad
   portal profile, the SHA-256 of the controller's exact `/etc/machine-id` file
   bytes, exact accepted image identity, and the final USB-absence probe.
   Record the exact eight-component hardware inventory. List every camera,
   validator, and USB-probe support/config/reference file in its corresponding
   `*_support_files` array; interpreter scripts must be canonical absolute paths
   and must be listed. Copy the resulting exact canonical command and file
   hashes into the signed safety gate. Secrets remain only in this private
   mode-0600 file and are never copied into artifacts. Application-release,
   lab-approval, and image-release signing private keys remain off both Pis.
6. Inspect then simulate:

```text
tidetracker-hil --config <private-config>
tidetracker-hil --simulate --config <private-config> --cycles 2 \
  --expected-release-id built-in --expected-execution-source built-in \
  --artifact-dir <new-dir>
```

## Real cycle

After explicit hardware approval:

```text
tidetracker-hil --execute --execute-confirmation EXECUTE-TIDETRACKER-HIL \
  --safety-gate <approved.json> --approval-nonce <signed-random-nonce> \
  --expected-release-id <exact-id> \
  --expected-execution-source built-in|verified-release \
  --config <private-config> --cycles 1 --artifact-dir <new-dir>
```

For a candidate, add the exact prebuilt signed ARMv6 bundle with `--bundle` and
set its manifest release ID/source `verified-release`. Upload is hashed, SFTP
partial+atomic-renamed, verified/staged/activated by the fixed root helper, and
removed by the updater. The HIL result must show the same release ID/source,
successful selected run, no fallback, no rollback, and the same staged bundle
digest.

For each RUN, the fixture returns monotonic timestamps for the actual power-on
and DONE edges. Their difference is stored as `cold_wake_to_done_seconds`; the
inner Python timing is diagnostic only. DONE defaults to the signed conservative
400–600 ms window around legacy 500 ms. A shorter gate is accepted only when the
signed approval record explicitly says isolated hardware timing was validated.
Set `wifi_observation.target_mac` to the Zero's fixed WLAN MAC. The Pi 3 polls
its AP station table from before target power-on through the fixture-mapped DONE
edge and integrates association intervals in the controller monotonic clock.
The conservative power-on-to-DONE `wifi_power_budget_seconds` is the primary
promotion metric; association polling proves the radio path was exercised. The
integrated association interval, runtime observation, and phase sums remain
diagnostic and cannot promote.

After DONE the controller proves SSH disappears and both rails are <0.1 V,
photographs the retained panel, boots SETUP, and collects the exact durable run
result, matching metrics and supervisor evidence. `systemd-resolved`, NM resolver
integration, USB NM isolation, and active nft rules are all checked. Current
journal output is diagnostic only.

## Required campaigns

- Reliability: use `--accelerated-cycles` after a one-cycle checkout. This fixes
  the campaign at exactly 12 no-interval cold cycles and marks it distinctly
  from ordinary 20/20 benchmark sessions.
- Wi-Fi matrix: `--wifi-matrix`. The 12-case portal submatrix exercises the full
  cross-product of visible/hidden, open/WPA Personal/WPA Enterprise, and
  `/api/network/test` (associate, test, then save)/`/api/network/force-save`.
  Thus every hidden security mode has a real association case. Every HTTP
  acknowledgement completes before the controller changes radio mode. The
  target proves a root-owned mode-`0600` NetworkManager profile using only an
  SSID hash in evidence. An intentionally bad profile must fail and restore the
  prior active profile. Target and controller artifact scans must report zero
  credential matches. The fault matrix then creates a clean cached-data
  precondition. Bad credentials and client↔AP are exercised through the real
  setup portal and immutable controller profiles while USB SSH stays reachable.
  Disconnect, DNS, TLS, and injected HTTP 503 each run as a production cold
  cycle, must produce the expected cached/offline framebuffer and DONE, consume
  the exact one-shot fault, and then pass a clean cold recovery. Generic external
  exit-zero hooks are not accepted.

  For each portal case the orchestrator sends the private profile only through
  bounded stdin to the root controller helper. The helper stages root-only files
  under `/run/tidetracker-portal-matrix`; a fixed foreground service uses
  hostapd plus its internal PEAP/MSCHAPv2 server, dnsmasq, and an nftables NAT
  rule to provide the exact open, WPA Personal, or WPA Enterprise 2.4 GHz AP.
  Its stdout/journal are disabled, its public receipt contains no SSID or
  credential, and reset removes the runtime tree before restoring the normal
  lab AP. Ethernet remains the controller management/upstream path.
- Power loss: repeat `--power-cut-phase` or use `--power-cut-phase all`. Fixture
  firmware parses schema-1 UART phase events amid early boot console text,
  returns the requested phase/edge, cuts power, then proves cold recovery.
- DONE crash boundaries: before release approval, use the isolated fixture to
  kill the sole DONE owner before intent, after intent/before LOW, immediately
  before and after HIGH, before and after final LOW, before completion-marker
  recording, at the outer command timeout, and during systemd stop. Evidence
  must show at most one rising edge and a successful cold recovery in every
  case. An ambiguous intent is deliberately never retried in software.
- Soak: optionally `--soak-hours 24|72`; interval is 60–3600 seconds from config.
- Field: physically disconnect USB, obtain approval, then run `--field-cycle`
  alone. The USB-absence probe must pass before and while powered; evidence is
  fixture/UART/rails/camera. UART must report the expected release/source and
  deterministic image build ID, while the session binds the externally signed
  whole-image and role-manifest hashes.

Update-phase power cuts require the exact candidate `--bundle` and their own
campaign, for example `--update-power-cut-phase all`. The target helper advances
the signed bundle through `begin`, `stage`, `verify`, or `activate`, emits a
bounded UART boundary, and blocks until the fixture removes power. Each cold
SETUP recovery must prove the expected durable updater state, clean it back to
a safe baseline, report no fallback, re-check rails, and bind an accepted camera
frame. Lifecycle cuts do not claim this coverage. The session exposes separate
`update_power_cut_phases` and `update_power_cut_recoveries` fields.

## Legacy visual and 20/20 promotion

`plot_image.bmp` plus `tidetracker display-oracle` verifies panel orientation
and driver transform, but it does not establish legacy renderer equivalence.
Exact parity requires at least one human-approved legacy hardware capture bundle
containing frozen clock, normalized model, raw NOAA responses, asset hashes, and
the exact 48,000-byte legacy controller framebuffer. Run the repository legacy
replay verifier; an empty/capture-required manifest fails closed. Its report and
capture manifest are signed by the pinned approval key and bound to the exact
candidate release ID and bundle SHA-256.

Each captured `raw_noaa_responses` entry must record `"time_basis":"lst_ldt"`,
matching the legacy request. The verifier interprets those naive NOAA timestamps
in the configured station timezone and rejects ambiguous or nonexistent civil
times; DST transition correctness is covered by the separate UTC-reference
fixtures rather than inferred from an ambiguous legacy response.

Run at least 20 baseline and 20 candidate cycles in alternating order (for
example, alternating one-cycle session directories). Then evaluate offline;
repeat `--baseline`/`--candidate` for every benchmark session and
`--release-evidence` for each independently signed auxiliary campaign:

```text
tidetracker-promote --baseline <b01> --candidate <c01> ... \
  --release-evidence <lifecycle-cut-session> \
  --release-evidence <update-cut-session> \
  --release-evidence <wifi-portal-session> \
  --release-evidence <accelerated-12-session> \
  --release-evidence <field-usb-disconnected-session> \
  --expected-baseline-release-id built-in \
  --expected-baseline-execution-source built-in \
  --expected-candidate-release-id <candidate-id> \
  --expected-candidate-execution-source verified-release \
  --expected-candidate-bundle-sha256 <64-hex> \
  --legacy-report <signed-legacy-report.json> \
  --image-manifest <signed-zero-role-image-manifest.json> \
  --image-release-public-key <accepted-image-release-public-key.pem> \
  --workstation-to-controller-ssh-public-key <accepted-workstation-controller.pub> \
  --controller-to-zero-ssh-public-key <accepted-controller-zero.pub> \
  --application-release-public-key <accepted-application-release.pem> \
  --lab-safety-public-key <accepted-lab-safety.pem> \
  --expected-image-release-public-key-sha256 <64-hex> \
  --expected-image-manifest-sha256 <64-hex> \
  --expected-image-sha256 <64-hex> \
  --expected-image-build-id <64-hex> \
  --expected-openssl-sha256 <trusted-pi3-openssl-64-hex> \
  --output <decision.json>
```

The OpenSSL digest is the separately accepted hash of the controller's pinned
ARM `/usr/bin/openssl`, recorded during trusted controller provisioning. Do not
derive it inline from the executable whose identity the promotion run is meant
to verify; the Windows flasher uses its own independently accepted verifier hash.

The evaluator verifies the report `.sig` with the image-pinned approval key,
candidate/bundle binding, staged bundle hashes, supervisor source/no-fallback
evidence, strict alternation, zero swap, <128 MiB RSS, refresh count, p95
ceilings, and the 95% bootstrap/practical-improvement gate. It uses fixture cold
wake and actual Wi-Fi observation, returns nonzero on rejection, and never
activates/promotes a release itself.

Every physical HIL `session.json` used by promotion is signed out of band with
the pinned lab-approval key and kept beside its `.sig`. The orchestrator does not
hold a private signing key. The evaluator rejects simulated, unsigned, or
identity-mismatched campaign evidence. Every camera record repeats the signed
capture and validator identities, and every field USB probe record repeats the
signed probe identity. Promotion rejects a substituted executable, script,
configuration/reference file, command line, device inventory, or evidence
record even if the substituted command exits successfully.

## Failure boundary

Unsafe rails, host-key changes, missing resolver/firewall evidence, invalid or
reused run IDs, release fallback, unconsumed/no-op faults, out-of-gate DONE,
camera rejection, empty/error UART evidence, incomplete updater or portal
coverage, credential leakage, non-TIDEDATA artifacts, unsigned/stale legacy
evidence, and controller reset failure all fail closed. Camera and UART files
are recorded by size and SHA-256 so a signed session cannot refer to mutable,
unbound auxiliary evidence. Cleanup requests power off; a dead fixture or
non-booting SD still requires hands-on recovery because v1 has no SD multiplexer.
