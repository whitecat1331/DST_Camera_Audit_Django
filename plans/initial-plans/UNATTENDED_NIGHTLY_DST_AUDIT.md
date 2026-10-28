# Initial plan — Unattended scheduled DST audit on a pre-connected VPN

Discussed: 2026-10-28, in the DST shared-review thread.
- The user asked: "when I select all eligible, my DST project knows how to route the traffic if it is a VPN call or not … set this as a schedule … let it run over night, so it can't require human intervention."
- Then: "We have to use the option that doesn't require IT intervention."
- Then: "MFA yes it does for the initial connection. I am thinking I connect to the VPN before and then DST will route traffic accordingly instead of initiating the connection. Schedule, one time fire at a specific time according to the time set on the local device."

Stage: plan
Related:
- plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md (publish + assignment + review; its "Final acceptance — a real audit day" is the manual version of this plan's final acceptance)
- in-progress-plans/CBW_FIRMWARE_INVENTORY_REPORT.md (no overlap; also probes CBWs over the camera VPN)

## Problem
A DST audit needs two networks that can't be up at once:
- **Camera VPN on:** LTI sites (CBW relays at `10.x.y.177`, VNC lanes) are only reachable through the camera VPN.
- **VPN off:** DE sites are captured with the VPN off (user practice since 2026-10-27), and **publishing to Supabase is impossible on the VPN**. Plan I2 in DST_AUDIT_SHARED_REVIEW: the VPN blocks 5432/6543, and IT won't open ports.

Today a person switches the VPN by hand between the LTI run, the DE run and publish. That blocks a scheduled, unattended run.

**Evidence (laptop, 2026-10-28, read-only):**
- **VPN client:** WatchGuard Mobile VPN with SSL (`C:\Program Files (x86)\WatchGuard\…\wgsslvpnc.exe`, service `wgsslvpnsrc`, TAP-Windows V9 adapter). It is OpenVPN underneath and bundles `openvpn.exe`. The client log shows `OVPN:>LOG:` lines, i.e. the GUI drives `openvpn.exe` through OpenVPN's management interface.
- **Profile** (`%APPDATA%\WatchGuard\Mobile VPN\client.ovpn`): `redirect-gateway def1` (full tunnel), `proto tcp-client`, `remote vpn.bluelinesolutions.org 444`, `auth-user-pass`, `reneg-sec 28800` (8 h).
- **DE capture** (`services/teamviewer_capture.py`) drives TeamViewer windows (window enumeration + PrintWindow), so it **needs a signed-in, unlocked interactive desktop**. A non-interactive scheduled task (session 0) can't do it.
- **Fleet run** (`audits/runner.py:2173` `_run_dst_audit`):
  - Phase 1 mixes LTI CBW relays (VPN) with one OvrC login for DE power-on.
  - Phase 2 is one shared boot wait.
  - Phase 3 captures every site in one loop.
  - Phase 4 is a fleet OvrC time pass.
  - The runner has no notion of which network each step needs.

**Constraints (user, 2026-10-28):**
- **No IT involvement.** No split tunnel, no Firebox policy change, no new ports.
- **The WatchGuard login uses MFA for the initial connection**, so DST can never start the VPN unattended. **The user connects the VPN beforehand** (with MFA), and DST works with that connection instead of starting it.
- **The schedule is a one-time fire at a set time on the device's local clock.**

**Not done: local route bypass.** Host routes that send Supabase/TeamViewer around the full tunnel while it's up would locally defeat IT's `redirect-gateway` setting, which is the split tunnel in all but name. They stay out unless IT approves.

## Proposed approach
DST never connects the VPN. At fire time it **detects** the network state, runs the VPN half while the user's connection is up, then **releases** the VPN once (disconnecting needs no MFA; only connecting does) and runs the direct half. The order is always VPN, then direct, never back.

- **Network requirement per step:**
  - `camera_vpn`: CBW relay on/off, CBW/VNC capture.
  - `direct`: Supabase publish (blocked on the VPN), IMS sync.
  - **To measure (Phase 0):** OvrC (HTTPS 443) and TeamViewer (5938, falls back to 443). If both work on the VPN, the **whole capture runs on the VPN** and only publish needs it off.
- **Network detector (`services/netstate.py`):** probes classify the state as `camera_vpn` (a known CBW `10.x.y.177` answers), `direct` (Supabase answers), or `none`. It is checked before each phase, never assumed.
- **Releasing the VPN after the VPN half**, tried in order:
  1. **Programmatic disconnect of the WatchGuard session:** `signal SIGTERM` over the WatchGuard `openvpn.exe` management socket, if it is a local socket DST may use. Otherwise use the WatchGuard client's own disconnect, if it has an unattended form. Phase 0 finds out.
  2. **If it can't disconnect:** DE captures that work on the VPN still run. **Publish is held** in a pending state, and a watcher **publishes automatically as soon as the VPN is seen off** (e.g. when the user disconnects in the morning). The morning card says "waiting for VPN off to publish".
- **If the VPN is down at fire time** (never connected, or dropped overnight; the 8 h `reneg-sec` may re-prompt MFA and drop the tunnel): the LTI half is **skipped and reported**, and the direct half (DE + publish) still runs. The schedule page warns when the fire time is more than ~7 h after the VPN connected.
- **Run order** (same code for the manual "all eligible" button and the schedule):
  1. **[VPN]** LTI relays on (+ OvrC DE power-on if OvrC works on the VPN) → boot wait → LTI captures (+ DE captures if TeamViewer works on the VPN).
  2. **Release.**
  3. **[direct]** the remaining DE work → OvrC time pass → publish to the round (assignment happens at publish, as now).
- **One-time schedule on the device's local clock:**
  - The schedule page takes a date + time (device local time, shown with the device's zone and current clock), scope/state/agency, the round name and publish on/off.
  - Saving it **registers a one-time Windows Task Scheduler trigger** (`/SC ONCE` at that local date/time; "wake the computer"; "only when user is logged on", because TeamViewer needs the desktop). Task Scheduler fires it on the device clock, so time changes are handled by Windows.
  - The task runs `manage.py run_scheduled_audit <id>`: a lock stops double starts, the run is recorded, and the task is deleted after it fires.
  - Editing or cancelling the schedule updates or removes the task. "Run now" uses the same path.
- **Readiness (`manage.py dst_preflight`)**, at save time and again at fire time:
  - an unlocked interactive desktop, and TeamViewer running;
  - Chrome/driver present;
  - the network state (warn if the VPN isn't up);
  - disk space;
  - no sleep on AC.

  A failed check is recorded as the run's outcome, never a silent skip. A "last run" dashboard card shows each phase's results and the publish state, and the published round appears on :8443 for the reviewers.

**Rejected / not available:**
- **DST connecting the VPN:** the initial connection needs MFA.
- **Split tunnel or a local route bypass:** policy; see above.
- **HTTPS ingest via the Linode while on the VPN:** the VPN only allowed 443 among the ports tested, and 443 belongs to IMS (I3 in the shared-review plan).

## Execution phases
Each phase is an acceptance block.

### Phase 0 — Spike: what works on the VPN, and can DST release it? (gated)
Throwaway code in `tmp/`; it never ships. Answers two questions:
1. **Reachability matrix**, with the user's VPN connected and then disconnected:
   - a known CBW and a VNC lane;
   - OvrC (login page);
   - **TeamViewer** (a real DE session opens and a capture works);
   - Supabase 5432/6543;
   - IMS;
   - :8443.
2. **Release:**
   - Does the WatchGuard `openvpn.exe` expose a management socket on localhost (`netstat` while connected), and does it need a password?
   - Does a `signal SIGTERM` over it, or the WatchGuard client's own disconnect, end the tunnel with no prompt and restore routes?
   - Does the WatchGuard GUI then behave (no auto-reconnect, no error loop)?
   - Admin rights needed?
- **Accepted when:** the matrix is recorded for every dependency, and the release question is answered yes or no with evidence. Either answer is fine: "no" selects the held-publish path.
- **Hard limit:** the user connects the VPN (MFA) for the test.

### Phase 1 — Network detector + VPN-half/direct-half run (gated)
1. `services/netstate.py` (probes, classification) and a single table of the network each step needs.
2. Refactor `_run_dst_audit` into the VPN half → release → direct half, ordered by the Phase 0 matrix. The boot waits follow the power-on groups.
3. The release step: programmatic disconnect if Phase 0 said yes; otherwise the held-publish state.
4. A setting `DST_NETWORK_PHASES` (off until accepted). With it off, behavior is exactly as today.
5. Progress shows the current network.
- **Accepted when:**
  - unit tests: phase order for each scope (all / lti / de); VPN down at start → LTI skipped and reported, DE + publish run; release fails → publish held;
  - a **live small run** from the button (VPN connected by the user, 2 LTI + 2 DE) completes with no manual action between halves and is published to a test round visible on :8443 (or held, then auto-published when the user disconnects);
  - the existing tests still pass.

### Phase 2 — Held publish + auto-publish on VPN off (gated; folded into Phase 1 if Phase 0 shows DST can always release)
1. A pending-publish record.
2. A watcher (a short Task Scheduler repeat, or the DST process) that publishes as soon as the state is `direct`.
3. The dashboard shows "waiting for VPN off".
- **Accepted when:** unit tests cover the state transitions; live: a held publish runs by itself within a minute of the user disconnecting.

### Phase 3 — One-time local-time schedule (gated)
1. `ScheduledAudit` model + migration (local SQLite): fire_at (device local), scope/filters, round name, publish, status, plus the run link.
2. Schedule page: create / edit / cancel / "Run now", with the device's current local time and zone shown.
3. Task Scheduler integration: register a `/SC ONCE` task on save (wake, interactive user), update/delete on edit/cancel, self-delete after firing.
4. `manage.py run_scheduled_audit <id>` (lock, preflight, run, record).
- **Accepted when:**
  - unit tests: the task XML for a given local datetime (including the DST-change night), edit/cancel replaces/removes the task, a double fire is refused;
  - live: a schedule set ~10 min ahead **fires by itself** with nobody touching the laptop, runs, publishes, and shows as done; the task is gone afterwards.

### Phase 4 — Readiness + morning report (gated)
1. `manage.py dst_preflight` (the checks in the approach), at save and at fire.
2. The "last run" card.
3. Operator notes in `README.md`: connect the VPN before leaving, keep the laptop plugged in, unlocked and awake, TeamViewer signed in, and keep the fire time within ~7 h of connecting.
- **Accepted when:** each preflight failure is simulated (no desktop, TeamViewer closed, VPN down, sleep on) and recorded with a clear message; the card shows a real run.

### Final acceptance — a real scheduled run, no human after VPN connect
1. The user connects the VPN in the evening and sets a one-time schedule.
2. At the set local time DST runs all eligible sites, releases the VPN (or holds the publish), and publishes to the round, assigned to carthur/jgreene/ployd.
3. Reviewers work it on :8443 the next morning.

## Issue digest
None yet.

## Open questions
- ~~Q1 — MFA~~ — **resolved 2026-10-28 (user):** yes, on the initial connection. The user pre-connects the VPN, and DST never connects it.
- ~~Q3 — schedule~~ — **resolved 2026-10-28 (user):** a one-time fire at a set time on the device's local clock.
- **Q2 — which machine?** Presumably this laptop, since the user connects the VPN on it. It must stay plugged in, signed in, unlocked and awake, because a locked screen stops the DE (TeamViewer) captures.
- **Q4 — the 8 h renegotiation:** does the Firebox re-prompt MFA at `reneg-sec 28800` and drop the tunnel? It decides how late the fire time can be. Observe it during Phase 0, or the user may know.
- **Q5 — round per scheduled run:** a new round each time (recommended), or append to a named round (e.g. the time-change round, when LTI and DE are captured on different nights)?

## Status
open — not started. Stage `plan`; revised 2026-10-28 for MFA (the user pre-connects the VPN) and the one-time schedule.

## Checklist
- [ ] Phase 0 — spike: reachability matrix on/off VPN + can DST release the WatchGuard VPN (yes/no with evidence)
- [ ] Phase 1 — network detector + VPN half → release → direct half, live small run with no manual action between halves, accepted
- [ ] Phase 2 — held publish + auto-publish on VPN off, accepted (or folded per Phase 0)
- [ ] Phase 3 — one-time local-time schedule via Task Scheduler, fires unattended, accepted
- [ ] Phase 4 — preflight + morning report, accepted
- [ ] Final acceptance — a real scheduled run, no human after the VPN connect
