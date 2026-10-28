# Plan of action — Unattended scheduled DST audit on a pre-connected VPN (2026-10-28)

Discussed: 2026-10-28, in the DST shared-review thread.
- The user asked: "when I select all eligible, my DST project knows how to route the traffic if it is a VPN call or not … set this as a schedule … let it run over night, so it can't require human intervention."
- Then: "We have to use the option that doesn't require IT intervention."
- Then: "MFA yes it does for the initial connection. I am thinking I connect to the VPN before and then DST will route traffic accordingly instead of initiating the connection. Schedule, one time fire at a specific time according to the time set on the local device."

Stage: in progress (promoted 2026-10-28: the user said "It will be remote over vpn … Update the plan and start implementation")
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
3. **Long connection (Q4):** connect, leave the VPN idle past 8 h, and record whether it stays up or re-prompts MFA and drops (the WatchGuard log + periodic CBW probe, logged with timestamps).
- **Accepted when:** the matrix is recorded for every dependency, the release question is answered yes or no with evidence, and the 8 h behavior is observed. Either release answer is fine: "no" selects the held-publish path.
- **Hard limit:** the user connects the VPN (MFA) for the test.

### Phase 1 — Network detector + VPN-half/direct-half run (gated)
Per the Phase 0 matrix: LTI relays + captures need the camera network; OvrC works both ways; TeamViewer (DE) and publish need the VPN off.
1. `services/netstate.py`: reachability probes (camera network = a selected LTI CBW :80 answers; direct = the shared DB answers), VPN detection (`openvpn.exe` running) and `release_vpn()` (force-close `wgsslvpnc.exe`, then `signal SIGTERM` on the OpenVPN management port, default 127.0.0.1:1337, then wait for the tunnel to go).
2. `_run_dst_audit` always runs (I3): LTI relays (only if the camera network answers) → boot wait → **every LTI capture** → **release** → OvrC DCAM on for DE → boot wait → **every DE capture** → OvrC time pass. Camera network down at start → LTI skipped and reported.
3. Optional publish round on the start request; after the run finishes, publish waits for the shared DB (held publish, "waiting for VPN off") and publishes into the round. This is Phase 2 folded in, because Phase 0 showed DST can release the VPN.
3a. I2 fix: TeamViewer windows open before the connect are never taken as the new session's window.
4. ~~A setting `DST_NETWORK_PHASES` (off until accepted).~~ Removed 2026-10-28 (I3): the user wants this order always.
5. Progress shows the current network.
- **Accepted when:**
  - unit tests: phase order for each scope (all / lti / de); VPN down at start → LTI skipped and reported, DE + publish run; release fails → publish held;
  - a **live small run** from the button (VPN connected by the user, 2 LTI + 2 DE) completes with no manual action between halves and is published to a test round visible on :8443 (or held, then auto-published when the user disconnects);
  - the existing tests still pass.

### Phase 2 — Held publish + auto-publish on VPN off (folded into Phase 1: Phase 0 showed DST can release the VPN; the held state is the publish step waiting for the shared DB)
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

### Phase 5 — The review page picks up newly published results (gated)
The review instance learns about a publish without a refresh. Publish writes to Supabase from the laptop, so the review instance's in-process SSE broker (`audits/live.py`) never hears about it.
1. A light watcher on the review instance checks each round's published-job count / newest `created_at` every ~30 s: one query, and only while someone has that round open.
2. On a change it broadcasts `round_updated` over the existing SSE stream.
3. The page shows "New results published — N cameras added" and pulls in the new rows. A reload keeps the filters; the rows already on screen and an open notes editor are untouched.
4. A brand-new round appears in the round picker.
- **Accepted when:** unit tests cover change detection and no event when nothing changed; live: with a review page open on :8443, a publish from the laptop shows the banner within ~30 s, and the new cameras appear with their assignees.

### Final acceptance — a real scheduled run, no human after VPN connect
1. The user connects the VPN in the evening and sets a one-time schedule.
2. At the set local time DST runs all eligible sites, releases the VPN (or holds the publish), and publishes to the round, assigned to carthur/jgreene/ployd.
3. An open review page on :8443 shows the new results without a refresh, and the reviewers work it the next morning.

## Phase 0 results (2026-10-28, the user on the VPN, then released by DST) — accepted by the user's direction to implement (2026-10-28); Q4 moved to final acceptance
- **Reachability matrix** (`tmp/spike/matrix.py`):

  | Dependency | VPN on | VPN off (office Wi-Fi 10.100.30.0/24) |
  |---|---|---|
  | LTI CBW `10.3.38.177`, `10.6.59.177` :80 | OK | **OK** (traceroute via 10.100.30.1 → 10.100.60.10/.80 → camera, 5 hops) |
  | LTI VNC lanes :5900 | OK (266059 lane 2 timed out both ways: probably no lane 2) | OK |
  | Supabase pooler 6543 / 5432 | **blocked** | OK |
  | Review site :8443 | **blocked** | OK |
  | OvrC (HTTPS) | OK | OK |
  | TeamViewer 5938 | **blocked** (falls back to 443; the client reports online) | OK |
  | IMS :443 | OK | OK |
- **DE on the VPN: unreliable (user: "the VPN cannot sustain a connection over TV, I've ran into this before").** One-site DE audit 266007 (job 174):
  - L1 captured, but in a window titled **"FX1361 L2"**, so it is possibly the wrong session;
  - **L2 failed both 70 s connect tries**;
  - OvrC OK (−35 s).

  **Off the VPN (job 178): both lanes OK in 152 s**, with correct window titles; L2 connected in 10 s; OvrC −51 s. → **DE always runs off the VPN.**
- **DST can release the VPN unattended, with no admin and no MFA:**
  - The WatchGuard tray app (`wgsslvpnc.exe`) ignores a normal close: no main window; a polite `taskkill` was a no-op.
  - Force-closing it (user process) leaves the tunnel up (`openvpn.exe` runs under the `wgsslvpnsrc` service) but frees OpenVPN's management socket **`127.0.0.1:1337`** (no password; banner `>INFO:OpenVPN Management Interface Version 1`; one client at a time, so it was queued while the app held it).
  - `signal SIGTERM` over it → `SUCCESS: signal SIGTERM thrown` → within 6 s the TAP adapter is disconnected, the `0.0.0.0/1` + `128.0.0.0/1` routes are gone, `openvpn` has exited, and the default route is back on Wi-Fi.
  - Supabase, :8443 and TeamViewer 5938 are then reachable.
  - Client: OpenVPN 2.4.12. The Firebox itself pushes `redirect-gateway def1, block-outside-dns`.
  - **Side effect:** the WatchGuard tray app is closed afterwards, and the user re-opens it to connect next time (MFA).
- **8 h renegotiation (Q4): not yet observed.** Needs a long connection.
- **Housekeeping:** spike retry job 176/177 was orphaned when its tool call was interrupted (stopped at 15:00:02), and was cancelled by hand. All spike jobs (174/175, 176/177, 178/179) are local only; none were published.

## Issue digest
- **I1 — resolved 2026-10-28 (user): the laptop runs remote, over the VPN** ("It will be remote over vpn; that is why we have to do this"). Office Wi-Fi reaching LTI without the VPN doesn't apply. The run is LTI on the VPN → DST releases the VPN → DE + publish direct. Detection is by reachability, so a run that happens to be on the office network still works (cameras reachable, nothing to release).
- **I2 — open, fixed in Phase 1: an on-VPN DE capture used a window titled "FX1361 L2" for FX1260 L1** (job 174/175). Off the VPN the title was correct. A TeamViewer session window left open from earlier was taken as the new session's window, because the session finder accepts any `"<host> - TeamViewer"` window. Fix: windows that existed before the connect are excluded from the new session's window search. Resolved when the unit test passes and the Phase 1 live run's DE titles match their FX.
- **I3 — fixed, awaiting the live rerun: the first Phase 1 live run (job 180, 2026-10-28 15:49) ran the old interleaved order.** The user: "It should start with LTI, finish and disconnect VPN, then continue with DE. Not interleave or any other way." Evidence: the first child was DE 255039 (FX1173). Both lanes failed with "TeamViewer session failed" on the VPN, and the user cancelled the run. Two causes:
  1. The new order sat behind `DST_NETWORK_PHASES`. It was added to `.env` at 15:42, but the serving runserver process (PID 15636) started at 15:37, and `.env` is only read at startup, so the flag was off.
  2. Even with the flag on, the DE OvrC power-on still ran at the start, on the VPN. It timed out (15:50:23 `TimeoutException`).

  Fix: the flag is removed, so the strict order is the only order. `_run_dst_audit` is now: LTI relays → boot wait → every LTI capture → release the VPN → OvrC DCAM on for DE → boot wait → every DE capture → OvrC time pass. It's split into `_dst_power_on_lti`, `_dst_settle`, `_dst_capture_half` and `_dst_capture_site`. The unit test asserts the exact event sequence and the child job order (L1, L2, D1, D2). Suite 128 OK. **Resolved when** the user's rerun shows that sequence live.

## Open questions
- ~~Q1 — MFA~~ — **resolved 2026-10-28 (user):** yes, on the initial connection. The user pre-connects the VPN, and DST never connects it.
- ~~Q3 — schedule~~ — **resolved 2026-10-28 (user):** a one-time fire at a set time on the device's local clock.
- ~~Q2 — which machine~~ — **resolved 2026-10-28 (user):** **this laptop**. It must stay plugged in, signed in, unlocked and awake, because a locked screen stops the DE (TeamViewer) captures. **When the run finishes, the results must update the review page on the Linode automatically.** That means publish runs at the end of every scheduled run (Phase 1/2), and open review pages learn about the new cameras (Phase 5).
- **Q4 — the 8 h renegotiation** — **user: "we just need to test it to find out."** Not observed in Phase 0; the user directed implementation to start (2026-10-28), so the observation moves to the final acceptance (connect in the evening, fire > 8 h later is avoided until it's observed; the run logs the VPN state at fire time). Phase 0 includes a long-connection observation: connect, leave the VPN idle past 8 h, and record whether it stays up or re-prompts MFA. That sets the maximum gap between connecting and the fire time.
- ~~Q5 — round per run~~ — **resolved 2026-10-28 (user):** "the audit will include both for now". **One run covers LTI + DE and publishes into one new round** (default name `DST audit {local date}`, editable on the schedule).

## Status
in progress — Phase 0 accepted (matrix + release answered; Q4 carried to final acceptance by the user's direction). **Phase 1 implemented 2026-10-28, not accepted**: unit tests pass (`audits/test_network_phases.py`, 16 tests; full suite 115 OK). Waiting on the live small run, which needs the user on the VPN (MFA).

Phase 1 as built:
- `services/netstate.py`: `camera_network_up`, `direct_up`, `vpn_up` (`openvpn.exe` running), `management_port` (netstat for openvpn's loopback listener, else `DST_VPN_MGMT_PORT`), `send_sigterm`, `release_vpn` (never raises; returns released + detail).
- `audits/runner.py`: `_run_dst_audit` probes up to 3 selected LTI CBWs; down → LTI skipped and reported. Captures run LTI first; `_dst_release_vpn` runs before the first DE site (or at the end of an LTI-only run that publishes). A failed release is a warning and DE still runs. (Order revised by I3: DE power-on now also waits for the release.)
- Publish: an optional "Publish to round when done" field next to Start (stored in the run's selection file). After a successful run `_start_dst_auto_publish` runs `publish.publish_after_run` on its own non-daemon thread: a PUBLISH job that shows "Waiting for the VPN to be off to publish" until the shared DB answers (up to `DST_PUBLISH_WAIT_SECONDS`, 12 h), then publishes; on timeout it fails with how to publish by hand.
- I2: `capture_dragoneye_via_teamviewer` records the session windows open before the connect and `_find_session_window` excludes them. Known limit: if TeamViewer refocuses an already-open window for the same ID instead of opening a new one, the connect now fails rather than capturing; DST closes each session after its capture, so this needs a leftover window for the same FX.

**Phase 3 built ahead of Phase 1's acceptance, by the user's direction (2026-10-28: "I don't see the scheduler to schedule the audit on the DST audit page").** The dummy run is done through the schedule, so one live run accepts Phase 1 and Phase 3 together.
- `ScheduledAudit` (migration 0011): fire_at, scope/state/agency, installation_ids, publish_round, status, message, pid, job.
- `services/task_scheduler.py`: one-time task XML (StartBoundary in UTC so the fall-back night is unambiguous; InteractiveToken; WakeToRun; never starts late; runs on battery; `pythonw.exe manage.py run_scheduled_audit <id>`), register/delete/run-now via `schtasks`, no admin needed.
- `audits/scheduling.py` + `run_scheduled_audit`: claims the row once (a double fire is a no-op), refuses while another DST audit runs, runs the audit in its own process, mirrors a Cancel from the DST page through the job's DB status, waits for the held publish, records the outcome and deletes its task.
- `reap_orphaned_audit_jobs` (runserver start) now spares a scheduled run's jobs while its process is alive, and fails the schedule if the process is gone.
- DST page: "Or run unattended at" (datetime-local on the laptop clock, with the clock shown) + "Schedule (N)" for the current selection and round, and a "Scheduled audits" table with Run now / Cancel. Edit = cancel and schedule again.
- Unit tests: `audits/test_scheduling.py` (13); full suite 128 OK.
- **Live fire-path check (2026-10-28 15:41):** a throwaway schedule with no eligible sites was registered 75 s ahead; Task Scheduler fired it on the laptop clock with nobody touching it, the command loaded `.env`, refused with "Not started: None of the selected sites are eligible", and deleted its task. The row was deleted afterwards. A real audit has not been fired by the scheduler yet.

**Dummy run sites** (re-picked 2026-10-28 by the user's rule: "look at IMS's sensor data … make sure all sensors are online"; Malibu is paused, so the first pick was wrong). Source: PRTG `table.json` (the feed IMS's enclosure sensors sync from), read-only, because IMS's web login now needs MFA and the VPN blocks SSH to the Linode. Every PRTG sensor in each site's pole group(s) is **Up**, and for LTI the CBW :80 and both VNC lanes also answered over the VPN at pick time:
- LTI 210073 (Springhill LA), 220054 (Shreveport LA), 220008 (Chatham County GA): Modem, CBW, Solar all Up.
- DE 266007 (FX1260, York VA; both lanes captured today), 255039 (FX1173, Whigham GA), 244127 (FX1071, Hollywood FL): Modem + both lane CPUs Up (244127 also two Axis cameras); 2 TeamViewer lanes each.
- Recheck the statuses right before the run; PRTG changes.

**Live run to accept Phase 1** (user): connect the VPN (MFA), select 2 LTI + 2 DE, type a test round in "Publish to round when done", Start, and don't touch the laptop. Accepted when LTI captures happen on the VPN, the VPN drops by itself, both DE sites capture with titles matching their FX, and the round appears on :8443.



## Checklist
- [x] Phase 0 — spike: reachability matrix on/off VPN + can DST release the WatchGuard VPN (yes, evidence above; Q4 carried to final acceptance)
- [ ] Phase 1 — network detector + VPN half → release → direct half, live small run with no manual action between halves, accepted (implemented; unit tests pass; live run pending)
- [x] Phase 2 — folded into Phase 1 (Phase 0: DST can release the VPN)
- [ ] I2 — stale TeamViewer window guard, verified in the Phase 1 live run
- [ ] Phase 3 — one-time local-time schedule via Task Scheduler, fires unattended, accepted (implemented ahead of Phase 1 by user direction; fire path checked live; real scheduled run pending)
- [ ] Phase 4 — preflight + morning report, accepted
- [ ] Phase 5 — review page picks up newly published results live, accepted
- [ ] Final acceptance — a real scheduled run, no human after the VPN connect
