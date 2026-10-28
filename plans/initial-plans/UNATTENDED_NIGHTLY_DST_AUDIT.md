# Initial plan — Unattended nightly DST audit with automatic VPN switching

Discussed: 2026-10-28, in the DST shared-review thread. The user asked: "when I select all eligible, my DST project knows how to route the traffic if it is a VPN call or not … set this as a schedule … let it run over night, so it can't require human intervention." Then: "We have to use the option that doesn't require IT intervention."
Stage: plan
Related:
- plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md (publish + assignment + review; its "Final acceptance — a real audit day" is the manual version of this plan's final acceptance)
- in-progress-plans/CBW_FIRMWARE_INVENTORY_REPORT.md (no overlap; also probes CBWs over the camera VPN)

## Problem
A DST audit needs two networks that can't be up at once:
- **Camera VPN on:** LTI sites (CBW relays at `10.x.y.177`, VNC lanes) are only reachable through the camera VPN.
- **VPN off:** DE sites are captured with the VPN off (user practice since 2026-10-27), and **publishing to Supabase is impossible on the VPN**. Plan I2 in DST_AUDIT_SHARED_REVIEW: the VPN blocks 5432/6543, and IT won't open ports.

Today a person switches the VPN by hand between the LTI run, the DE run and publish. That blocks a scheduled overnight run.

**Evidence (laptop, 2026-10-28, read-only):**
- **VPN client:** WatchGuard Mobile VPN with SSL (`C:\Program Files (x86)\WatchGuard\…\wgsslvpnc.exe`, service `wgsslvpnsrc`, TAP-Windows V9 adapter). It is OpenVPN underneath and bundles `openvpn.exe`.
- **Profile** (`%APPDATA%\WatchGuard\Mobile VPN\client.ovpn`): `redirect-gateway def1` (full tunnel), `proto tcp-client`, `remote vpn.bluelinesolutions.org 444`, `auth-user-pass` (username + password, no `static-challenge`), `reneg-sec 28800`. Nothing in the client log mentions MFA, SAML or AuthPoint, but a server-side push can't be ruled out from the laptop.
- **DE capture** (`services/teamviewer_capture.py`) drives TeamViewer windows (window enumeration + PrintWindow), so it **needs a signed-in, unlocked interactive desktop**. A non-interactive scheduled task (session 0) can't do it.
- **Fleet run** (`audits/runner.py:2173` `_run_dst_audit`):
  - Phase 1 mixes LTI CBW relays (VPN) with one OvrC login for DE power-on.
  - Phase 2 is one shared boot wait.
  - Phase 3 captures every site in one loop.
  - Phase 4 is a fleet OvrC time pass.
  - The runner has no notion of which network each step needs.

**Constraint (user, 2026-10-28):** **no IT involvement.** No split tunnel, no Firebox policy change, no new ports. DST switches the existing VPN itself.

## Proposed approach
DST knows which network each step needs and switches the existing WatchGuard VPN profile itself, then checks that the switch worked. A scheduler runs the whole thing overnight.

- **Network requirement per step:**
  - `camera_vpn`: CBW relay on/off, CBW/VNC capture.
  - `direct`: TeamViewer capture, Supabase publish, IMS sync.
  - `either`: OvrC (HTTPS 443); Phase 0 confirms this.

  Every step declares its requirement, and the runner orders a run into **network phases** so it switches as few times as possible.
- **VPN controller (`services/vpn.py`):**
  - Runs the bundled `openvpn.exe` against **a fresh copy of the WatchGuard profile**, re-copied each run so a Firebox-pushed profile change is picked up.
  - Uses OpenVPN's **management interface** (`--management 127.0.0.1 <port>` + `--management-query-passwords`). Credentials are answered over the socket from a **Windows DPAPI**-encrypted store (current user), so there's **no plaintext auth file on disk** and nothing in `.env`.
  - `ensure("camera_vpn" | "direct")` is idempotent and checks the result with probes: a known CBW answers on VPN; Supabase answers off VPN.
  - Retries with backoff. If it fails, it skips that network's phase and records why; it never hangs.
  - A context manager **always leaves the VPN disconnected** at the end of a run, including after a crash. Next startup also tears down a stranded tunnel.
  - Detects the WatchGuard GUI holding the TAP adapter and refuses with a clear message.
- **Network-phased run (same code for the manual "all eligible" button and the scheduler):**
  - Ordering, if Phase 0 shows OvrC works on the VPN: **VPN on** → LTI relays on + OvrC DE power-on → one boot wait → LTI captures (CBW/VNC) → **VPN off** → DE TeamViewer captures → OvrC time pass → publish to a round (assignment happens at publish, as now).
  - If OvrC doesn't work on the VPN, the DE power-on moves after the disconnect and gets its own boot wait.
- **Scheduler:**
  - `Schedule` / `ScheduleRun` models and a schedule page: days and time, scope/state/agency, a round-name template such as `Nightly {date}`, a publish switch, and enabled.
  - One **Windows Task Scheduler** entry runs `manage.py run_due_schedules` every 15 min ("wake the computer", "run with highest privileges", "only when user is logged on", because TeamViewer needs the desktop). DST decides what's due.
  - A lock prevents overlapping runs. A run interrupted by a reboot is marked and, if still in its window, retried.
  - `manage.py install_schedule_task` registers or updates the task, so nobody hand-edits Task Scheduler.
- **Unattended readiness:** `manage.py dst_preflight` checks before each run:
  - an interactive desktop, and TeamViewer running;
  - Chrome/driver present;
  - VPN credentials stored and the profile present;
  - disk space;
  - power plan on AC: no sleep.

  A failed preflight is a recorded run failure, not a silent skip. A "last night" summary goes on the dashboard, and the published round shows up on :8443 for the reviewers.

**Rejected / not available:**
- **Split tunnel** (route only 10.x through the tunnel): it overrides IT's full-tunnel policy, ruled out by the user.
- **HTTPS ingest via the Linode while on the VPN:** the VPN only allowed 443 among the ports tested, and 443 belongs to IMS (I3 in the shared-review plan).
- **Driving the WatchGuard GUI client (`wgsslvpnc.exe`):** it has no documented unattended command line, and GUI automation is fragile.

## Execution phases
Each phase is an acceptance block.

### Phase 0 — Spike: can the VPN be switched unattended? (gated; it decides whether the plan is feasible)
A single falsifiable question: from a non-GUI process, can the bundled `openvpn.exe` + a copy of the profile + management-interface credentials connect and disconnect **with no human step**?
1. **One-time setup:** the user types the VPN password into a DPAPI store (a credential the agent can't access; a hard limit).
2. **Connect/disconnect 3×:** each time, record:
   - connect time;
   - whether any MFA/push or prompt appeared;
   - the probes: a known CBW `10.x.y.177`, Supabase 5432/6543, OvrC, TeamViewer, IMS, the review site :8443;
   - the routes after disconnect;
   - whether admin rights were needed;
   - interaction with the WatchGuard GUI and its service.
3. **Output:** a **reachability matrix** (on VPN vs off for each dependency), which fixes the run ordering.
- **Accepted when:** 3/3 unattended cycles succeed with no prompt, camera reachable on VPN, Supabase reachable after disconnect, routes restored. **If the login needs MFA/push, stop:** an unattended connect is impossible without IT, which the user ruled out. Record it and return to the user.
- The spike code is throwaway (`tmp/`) and never ships as the feature.

### Phase 1 — VPN controller + credential store (gated)
1. `services/vpn.py`:
   - controller with `ensure()`, probes, retry/backoff and the always-disconnect context manager;
   - stranded-tunnel cleanup;
   - WatchGuard-GUI-active detection;
   - profile copy and refresh.
2. `manage.py vpn_credentials set|test|clear` (DPAPI; never printed).
3. `manage.py vpn_check` (connect → probe → disconnect → probe, prints the matrix).
4. **Network requirements:** each runner step declares one, in a single table with no scattered booleans.
- **Accepted when:**
  - unit tests with a fake `openvpn` (management-protocol replay): connect, auth, timeout, crash mid-run → disconnected, GUI-holds-adapter refusal, probe failure → phase skipped;
  - live: `vpn_check` passes 3× in a row;
  - nothing secret appears in logs or on disk (grep the logs and the profile copy for the password).

### Phase 2 — Network-phased "all eligible" run (gated)
1. Refactor `_run_dst_audit` into network phases using the Phase 0 matrix, with the VPN controller wrapped around each phase.
2. The boot waits follow the phases (one per power-on group).
3. Optional publish at the end (needs `direct`); the round name is passed in.
4. A setting `DST_VPN_AUTO` (default off until accepted). With it off, behavior is exactly as today (manual VPN).
5. The UI's "all eligible" start uses the same path when `DST_VPN_AUTO=1`, and progress shows the current network ("on camera VPN", "VPN off").
- **Accepted when:**
  - unit tests for phase ordering per scope (all / lti / de), and a phase skipped when its network can't be reached (the other phase still runs, and the errors are recorded);
  - a **live small run** started from the button (2 LTI + 2 DE selected) completes with **no manual VPN action**, the VPN is off at the end, and the run is published to a test round visible on :8443;
  - existing tests still pass.

### Phase 3 — Scheduler (gated)
1. `Schedule` / `ScheduleRun` models + migration (local SQLite only; schedules live on the capture machine).
2. Schedule page: create / edit / enable, "Run now", and run history.
3. `manage.py run_due_schedules` (lock, due calculation, interrupted-run recovery, runs the Phase 2 path synchronously in-process).
4. `manage.py install_schedule_task` (Task Scheduler XML: every 15 min, wake to run, highest privileges, interactive user).
- **Accepted when:**
  - unit tests: due calculation across days and DST changes (America/New_York), no double start under the lock, recovery after a killed run;
  - live: a schedule set 10 min ahead **fires through Task Scheduler** with nobody touching the laptop, runs, publishes, and shows in run history.

### Phase 4 — Unattended readiness + morning report (gated)
1. `manage.py dst_preflight` (the checks listed in the approach). A failure is recorded as the run's outcome.
2. Dashboard "last night" card: per-network phase results, sites ok/failed, published round, link to :8443.
3. Documentation in `README.md` / an ops section: laptop/VM setup (power plan, no lock or auto-sign-in, update hours, TeamViewer signed in).
- **Accepted when:** each preflight failure is simulated (no desktop, TeamViewer closed, no credentials, sleep enabled) and recorded with a clear message; the card shows a real run.

### Final acceptance — a real night, no human
A schedule runs overnight on the chosen machine:
1. all eligible sites, VPN switched automatically;
2. published to `Nightly <date>`, assigned to carthur/jgreene/ployd;
3. the next morning the reviewers work it on :8443;
4. the VPN is found disconnected and nobody touched the machine.

## Issue digest
None yet.

## Open questions
- **Q1 — MFA on the WatchGuard login?** If a push is sent to a phone (AuthPoint), an unattended connect is impossible without IT. Phase 0 answers it, or the user already knows.
- **Q2 — Which machine runs it overnight?** This laptop (it must stay signed in, unlocked and awake) or a dedicated always-on PC/VM (recommended: a locked screen stops the DE captures).
- **Q3 — Schedule and rounds:**
  - Nightly or only around a time change?
  - One round per night (`Nightly {date}`), or append to a named round?
  - Publish automatically every time? (Recommended: yes, a round per night.)
- **Q4 — Admin rights:** does the bundled `openvpn.exe` need elevation to add routes when run outside the WatchGuard service? Phase 0 answers it; if so, the task runs "with highest privileges".

## Status
open — not started. Stage `plan`; Phase 0 decides feasibility.

## Checklist
- [ ] Phase 0 — spike: 3/3 unattended VPN cycles + reachability matrix (or stop on MFA)
- [ ] Phase 1 — VPN controller + DPAPI credentials + `vpn_check`, accepted
- [ ] Phase 2 — network-phased "all eligible" run, live small run with no manual VPN action, accepted
- [ ] Phase 3 — scheduler + Task Scheduler entry, a schedule fires unattended, accepted
- [ ] Phase 4 — preflight + morning report, accepted
- [ ] Final acceptance — a real night, no human
