# Plan — Fix the run-389 findings: OvrC times, audit scope (legacy + PRTG-paused), solar power-on, CBW passwords

Discussed: 2026-10-30, in the run-389 cross-reference thread. The user asked for a plan:
"We do need to look into OVRC and why it didn't work. Some barrow sites and other LTI sites are legacy, those can be identified if they do not start with a 10.x IP and can be safely skipped. Next, I think the power on for solar sites is not working correctly so we need to look more into that. Identify the 16-19 poles that didn't work for the CBW password. Second, if the sensor has been paused in PRTG then there is no need to include it in the audit since we are not monitoring that installation ie 233047 and a bunch of others. Before the audit it should force IMS to do a PRTG update and then pull the sensor data from IMS to know which ones are paused."

Stage: plan (promoted 2026-10-30; in progress)
Related:
- plans/in-progress-plans/FAST_FLEET_DST_AUDIT.md: run 389 is effectively its Phase 5 full-fleet run. This plan's exclusions extend that plan's Phase 1 "skip what is known offline". Its Phase 5 acceptance should be re-run after this plan.
- plans/in-progress-plans/PER_DEVICE_TIME_CHECK.md: the per-device drift check depends on OvrC times. With the fleet OvrC pass broken, every DE site shows no OvrC reading.
- plans/in-progress-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md: the pre-audit IMS/PRTG refresh must run unattended, on the right network (VPN off) before the LTI half, or be skipped with a warning.
- plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md: owns the review page. This plan's Phase 4b (E6, columns fit the page) changes its Phase 6 table layout. Phase 4b should land after or alongside that plan's Phase 6 acceptance, not override it.
- IMS repo (`C:\Users\ployd\Desktop\IMS`): Phase 2 needs a token-scoped IMS endpoint and sensor status in the external API.

## Evidence (run 389, `FULL_TEST_DST_AUDIT_10:29:26`)
- **Run:** 814 sites from 18:32 to 00:22 ET. 604 succeeded, 157 skipped, 53 failed.
- **Cross-reference data:** `tmp/audit389/`
  - `sites.csv` is the per-site join of jobs, captures, IMS devices and PRTG.
  - The other files are the scripts and the PRTG snapshot from 2026-10-30 05:20 ET.
- **PRTG timestamps:** `lastup`/`lastdown` raw values are UTC.

### E1 — OvrC fleet pass recorded no times
- `ovrc_device_time` is null on all 284 DE child jobs: 236 succeeded, 48 failed.
- 13 OvrC-only sites failed only for that reason ("OvrC time not captured"), and 11 of them had modems up in PRTG.
- The pass ran for about 37 s for about 280 FX (last capture 04:21:44 UTC, run end 04:22:21 UTC). `media/audits/389/_ovrc_fleet/` is empty.
- The run's own records don't show the cause:
  - `logs/dst.log` stops at 19:08 ET. It reached its 10 MB `RotatingFileHandler` cap (`config/logging.py:72-77`), and no `dst.log.1` exists.
  - The exception `_dst_fleet_ovrc_times` returns as `"OvrC fleet: …"` (`audits/runner.py`) is lost. The parent `error_message` is truncated to 2,000 characters and is full of power-on errors.
- Earlier small runs did capture OvrC times: #375 got 6/12 and #351 got 6/12.

**Root cause (investigated 2026-10-30; read-only probes in `tmp/audit389/ovrc_probe*.py`, output in `tmp/audit389/ovrc_probe/`):**
1. **One unknown FX ends the whole pass.**
   - `capture_fx_times` (`services/ovrc_capture.py:1052`) loops over every FX in one session with no per-FX `try`. So the first FX that raises discards every result.
   - `_dst_fleet_ovrc_times` then returns `["OvrC fleet: …"]` and attaches nothing.
2. **The first FX queued was FX1031 (200049, Wolf Creek ES), and it is not an OvrC customer.**
   - OvrC-only sites are queued as soon as their turn comes (`_dst_queue_ovrc_only`). TeamViewer sites are queued when they finish. So 200049 was `de_ovrc[0]`.
   - OvrC's customer CSV (315 customers, `tmp/audit389/ovrc_probe/csv/`) lacks only 3 of the run's 291 DE FX:
     - **FX1031** (200049);
     - **FX1049** (220104, Houston Co HS);
     - **FX140** (I-VBE-0019). Its IMS record lists `FX140` and `FX1410`, so `FX140` looks like a bad parse or typo.
3. **Exact reproduction:**
   - `capture_fx_times(['FX1031','FX1051'], …)` raised `RuntimeError: OvrC search for FX1031 returned no customers` after **36.6 s**, with **0 files** written. Run 389's pass took about 37 s and left an empty folder.
   - FX1051 alone reads in 4.1 s, so login, search and reading the time all work.
4. **Small runs worked** only because their FX lists didn't include a missing FX.
5. **Log rotation:** the same probes hit `PermissionError: [WinError 32]` renaming `dst.log` → `dst.log.1` while the DST server held the file. That confirms Phase 0's cause.

### E2 — Legacy (non-10.x) sites
- 38 sites are skipped where IMS's CBW/TF CPU **device hosts** are public cellular IPs. Most are Barrow, GA (210xxx). The runner always dials `10.x.y.177` / `.140` (`services/ip_map.py`).
- Installation-level `ip_address` is the modem WAN IP and is public on most healthy sites. **The rule must use device hosts, not `ip_address`.**
- Two caveats:
  - **210071:** its modem is public, but its CBW and CPU are `10.21.71.x` and it captured fine. It stays in under a CBW/CPU-host rule.
  - **210017 and 210036:** IMS lists public hosts for them, yet they captured fine over `10.21.x`. A strict host rule drops them. See open decision D1.

### E3 — Solar power-on
LTI sites that ran (not skipped). The split that matters is **whether IMS lists a Victron Cerbo (`solar` device at `.170`)**, not IMS `power_type`:

| | relays turned on | relays already on |
|---|---|---|
| has Cerbo: VNC L1 missing / attempted | **60 / 71** | 59 / 134 |
| no Cerbo: VNC L1 missing / attempted | 3 / 9 | 9 / 159 |

- By `power_type`, Cerbo sites typed **AC** miss almost as often as those typed **Solar**:
  - AC + Cerbo: 53 / 88;
  - Solar + Cerbo: 54 / 93;
  - AC without a Cerbo: 7 / 133.
- **Rule (user, 2026-10-30): a site with a Cerbo/solar controller is a Solar site.**
  - IMS `power_type = AC` on the 129 Cerbo installations (128 poles; listed in the thread) is a data-entry error, and so is the blank `power_type` on 36 more. All 127 typed Solar do have a Cerbo, so the rule never contradicts a Solar entry.
  - So E3 is a solar problem: solar sites miss 119 / 205 and grid sites 12 / 168.
  - DST must derive "solar" from the Cerbo device, not from `power_type`.
- Of the Cerbo-site L1 misses, 70 TF CPUs were up in PRTG within the 24 h before the run, and 45 more within 7 days. These sites live by day and are dark in the evening.
- Toggling the CBW relays plus a 90 s settle (`DST_POWER_SETTLE_SECONDS`) does not bring them up.
- The code has no solar-specific path. `turn_all_relays_on` (`services/cbw_relays.py`) is the same for every site.

**Root cause (investigated 2026-10-30, read-only; scripts and data in `tmp/audit389/cbw_*`, `prtg_hist.*`):**
- **The CBW's own logic owns TF power on solar sites, not the relays.**
  - CBW scheduled tasks write register `register1` (I/O 10, named "All TruFix Power" on 143 CBWs and "Register 1" / "TF Reg 1" on the rest):
    - "Photo Speed Off" at 18:15 → 0;
    - "Photo Speed On" at 06:15 and "Midday Power On" at 12:00 → 1, if Vin is above 12.3 V / 12.5 V.
  - They run Monday–Friday (`daysOfWeek=62`), so power stays off all weekend.
  - About 38 conditional tasks (`condTasks.data`) run start-up and shutdown from that register. They use the TF A/B power registers (I/O 15/16), step timers of 300–365 s, low-voltage cuts at 12.2–12.5 V, and ping reboots (`rebootTasks.data`).
- **Evidence.**
  - **PRTG history.** The 7-day TF CPU ping history for 220116, 220122, 220084 and 220085 is up 06:15→18:15 site-local on weekdays and down at weekends.
  - **Live read.** At 06:04 ET on 220116 and 220122, all four relays were 1 (left that way by DST) while `register1`=0, every "Power to Unit" input was 0, and the CPU was down.
- **Why DST fails.** Writing `relay1..4=1` skips the logic. The relay ends up on but the unit stays unpowered, and the 90 s settle is shorter than the logic's own step timers.
  - The same call is used by the rejection power-on (`audits/runner.py:1675`).
- **Fleet sweep (read-only, 262 Cerbo poles).**
  - 190 read; 38 timed out; 31 rejected every manager password; 1 has no `10.x` CBW.
    - Correction (2026-10-30, E4): the 31 were the 429 lockout. On a lockout-aware retry (`e4_sweep_retry.json`), 29 accept manager password index 2. 220061 really rejects every manager password, though `state.json` accepts index 0. 233043 dropped its connection on the retry; its `state.json` accepts index 2. The schedule counts above don't include these 29 yet.
  - 142 have the 18:15 off schedule. Of those, 104 missed L1 (73%), and all were audited after their local 18:15.
  - 48 have no schedule. Of those, 7 missed (15%).
  - 89 CBWs are now in the state DST's writes left behind (relays `1111`, `register1`=0).
- **Open.**
  - 38 scheduled poles captured anyway, though audited after 18:15. Not yet explained.
  - One conditional task ("M2 Photo Speed Power On") appears to need Vin > 13.3 V before it sets the TF power registers. To be confirmed live.
- **User direction (2026-10-30).** The schedule is intended and stays. DST audits run at weekends, when the sites are scheduled off, so DST must be able to send the CBW a signal that actually powers the site on demand, without waiting for the schedule.

### E4 — CBW password rejected
- 19 sites were skipped as "CBW power-on failed twice" while PRTG had the CBW up for the whole run.
- None has a `Relay attempt failed` log line, and none has an entry in `cbw_password_hints.json`.
- The Oct 7 `cbw_firmware_report.csv` shows **16 as `auth_failed`**. The other 3 (220127, 244181, 255162) were `ok` on Oct 7.
- **Root cause (investigated 2026-10-30): login lockout, not a wrong password.** The user reported that the third `CBW_PASSWORDS` entry works on 233006 and 233043.
  - Probe: `tmp/audit389/e4_pw_probe.py`, `e4_lockout.py`, `e4_retest.py` → `e4_retest.json`. Read-only GETs, status codes only.
  - The CBW answers **429 after two failed logins** and keeps answering 429, even to the correct password, for about **20–30 s**.
    - Measured on 233006: one wrong password then the right one gives 200; two wrong then the right one gives 429; it cleared by +30 s.
  - All 19 need password index 2. Every one shows the same trail: 401, 401, 429, then **200** after a 35 s wait.
  - `services/cbw_relays.py::turn_all_relays_on` handles a 429 by sleeping 5 s and moving to the *next* password. Index 2 is the last one, so the loop ends and raises. The second power-on attempt fails the same way.
  - Same flaw elsewhere:
    - `services/cbw_firmware.py::read_cbw_firmware` treats a 429 as "try the next endpoint or password", which is why the Oct 7 report shows `auth_failed`. Index 2 working on 3 poles on Oct 7 was probably just timing.
    - My E3 solar sweep (`cbw_sweep.py`) had no 429 handling.
    - `services/cbw_capture.py::_probe_cbw_auth` has the opposite flaw: a 429 is below 500, so it counts as **accepted**.
  - Sites that succeed (such as 220116) accept index 0 for `state.json`, so they never reach the lockout.

| Pole | State | Agency | Power | Oct 7 report | TF CPU (PRTG, run window) |
|---|---|---|---|---|---|
| 220016 | GA | Savannah | AC | auth_failed | paused |
| 220113 | LA | Shreveport | AC | auth_failed | paused |
| 220127 | OH | Youngstown | AC | **ok (fw 3.12)** | up |
| 220145 | OH | Youngstown | AC | auth_failed | up |
| 220156 | OH | Youngstown | AC | auth_failed | down |
| 220196 | OH | Youngstown | AC | auth_failed | up |
| 220200 | VA | Wythe County | AC | auth_failed | down |
| 220205 | LA | Shreveport | AC | auth_failed | down later |
| 220206 | LA | Shreveport | AC | auth_failed | down later |
| 233006 | OH | Youngstown | AC | auth_failed | up |
| 233008 | VA | Smyth Co | Solar | auth_failed | down |
| 233011 | VA | Smyth Co | Solar | auth_failed | up |
| 233043 | VA | New Kent Co | Solar | auth_failed | up |
| 233049 | VA | New Kent Co | Solar | auth_failed | down |
| 233089 | LA | Shreveport | Solar | auth_failed | up |
| 234003 | VA | Fauquier | Solar | auth_failed | up |
| 244181 | GA | Villa Rica | — | **ok (fw 3.12)** | up |
| 255162 | TN | Burns | Solar | **ok (fw 3.12.2.5)** | down later |
| 266039 | TN | Woodbury | Solar | auth_failed | down later |

- 9 of these sites (TF CPU "up") had capturable cameras, but the skip rule dropped the whole site after the lockout.
- The "Power" column is IMS `power_type` as typed. 18 of the 19 have a Cerbo, so they are really Solar sites (see the E3 rule). Once past the lockout they also need the E3 method-B power-on.

### E5 — PRTG-paused and unmonitored
- 32 skipped sites are paused in PRTG. Another 25 have nothing capturable in IMS: 15 have no devices at all and 10 have no CBW or TF CPU.
- **233047** (`I-ASE-233047`) has no devices in DST's IMS copy and no PRTG device by that name.
- IMS already mirrors PRTG pause state:
  - `poleGroupAllSensorsPaused` (`IMS/assets_sync_normalize.go:444`) uses sensor `last_status`.
  - The paused-ancestor CTE is in `IMS/assets_sync_bridge_prtg_groups.go:28`.
- But IMS's external API doesn't expose it:
  - `aseExternalSensor` (`IMS/external_ase_devices.go`) has `last_value` but no status.
  - The sync triggers `/api/prtg-tree/sync` and `/api/enclosures/sync-sensors` are superuser-session only, not API-token.

### E6 — Review table scrolls sideways on the large run
- **User report (2026-10-30, screenshots).**
  - `/audits/review/15/` (run 389, 813 cameras): the Notes column is off the page and only reachable by scrolling sideways.
  - `/audits/review/14/` (SMALL_MIXED_TEST, 12 cameras): every column, Notes included, fits.
  - User: "I don't want a horizontal scroll to make comments, all columns should be formatted to the webpage."
- **Cause (from the code, `templates/audits/review.html:99-113`, `static/app.css`):**
  - The table is wrapped in `.table-scroll { overflow-x: auto }` with automatic table layout inside the 1200 px `.container`. So any row whose content is wider than the page widens the whole table and adds a scrollbar.
  - The run-389 rows are wider than the small test's:
    - **Status:** `.review-error` errors up to 160 characters at `max-width: 22rem` ("Not attempted: offline: CBW power-on failed twice", "VNC L1: timed out; VNC L2: timed out").
    - **Time columns:** three `white-space: nowrap` date-time columns (`.review-time`, about 11 rem each for "Oct 29, 06:42:34 PM EDT").
    - **Notes:** the `.review-notes` `min-width: 14rem`.
  - On the small test the errors are short, so the total still fits.

### E7 — Run 1220 (acceptance list, 2026-10-30 morning): solar LTI sites dark all day after DST's evening relay writes
- **User report.** In run 1220 many LTI sites failed, nearly all of them solar, even though the server was restarted before the audit.
- **Root cause (read-only, `tmp/audit389/r1220_*.py` / `.json`; CBW logs and `condTasks.data`).** The relay forcing in `turn_all_relays_on` stops the next morning's start-up.
  - The start-up chain on these CBWs (220001 type, decoded with `r1220_tasks.py`):
    - "R1 TF A Power Cycle": IF `trufixA1Relay`=0 AND `allTrufixPower`=1 THEN start the startup timer.
    - "R2 TF A Reboot": when the timer is done, set `trufixAPower`=1.
    - A1/A2/A3: relay on, off for 8 s, back on.
  - The 18:15 shutdown (S2/M3) turns the TF relay **off**, so R1 fires at 06:15.
  - When DST writes `relayN=1` after a site's local 18:15, the relay is on when `allTrufixPower` becomes 1 at 06:15. R1 never fires and the site stays dark all day.
  - The only other way to start is "M2 Photo Speed Power On". On these CBWs it is `systemVoltage` **< 12.8** (compare code 2 is "<"; see the D4 correction), so a healthy battery never takes that path.
  - Central-time sites escaped run 389 because DST reached them before their local 18:15.
- **Evidence.** Out of the 149 scheduled solar CBWs read (`r1220_stuck.json`, `r1220_verify.json`, `r1220_m2b.json`):
  - 49 were "stuck" (`allTrufixPower`=1, `trufixAPower`=0, A1 relay 1).
  - All 45 stuck ones with a usable log had been forced on by DST after their local 18:15 (run 389 and/or the 10-29 evening run), with no 06:15 cycle, and all have the M2 "< 12.8" gate.
  - The 8 forced sites that recovered have no M2 gate or a "< 13.3" gate.
  - 43 sites that weren't forced started normally at 06:15.
  - Not explained yet: 27 "running, relay on, no cycle" sites (mostly relays on since Aug/Sep), 14 "relay off, no cycle", 10 unusable logs, and 10 that read as stuck but answer VNC on lane A.
- **Why the earlier fix didn't cover it.** The method-B finding (E3, Phase 3) explained why DST's relay writes don't power a site on. It missed that the same writes, left in place, also block the CBW's own start-up the next day. Phase 3 hasn't been implemented, so run 1220 still used relay forcing.
- **Intervention test (user-approved, 2026-10-30; `relay_off_write.py`, `relay_off_watch.log`).** `customState.json?trufixA1Relay=0` (plus `trufixB2Relay=0` on two lanes):
  - 220001: relay off at 11:27:49. Its CBW set `trufixAPower`=1, ran the relay cycle, and VNC L1 was up at 11:28:34 (+45 s).
  - 233014: off at 11:27:53. Both lanes cycled and were up at 11:28:39 (+46 s).
  - 244091: off at 11:27:59. L1 was up at 11:28:46 (+47 s).
    - Lane B stayed off. 244091 has **no lane-B start-up tasks**, so its B2 relay is never switched back on by the logic. Lane B was dark before the write as well.
- **Recovery (user-approved "Yes and yes", 2026-10-30; `recover_stuck.py`, `recover_stuck.log`).** The other 46 stuck sites were handled per lane, after a fresh live read:
  - lane A: A1 relay → 0 only if still stuck and VNC `.140` was down;
  - lane B: B2 relay → 0 only if `trufixBPower`=0, the relay was on, VNC `.141` was down, and the CBW has lane-B start-up logic;
  - 9 sites were skipped because lane A already answered VNC and lane B had no logic.
  - **Result.** 37 sites were written, 11:32–11:40, all HTTP 200: 25 lane A only, 11 lanes A and B, and 1 (234014) lane B only.
    - **34 of 37 were fully up on VNC by 11:41:19**, within 4 minutes. The first answered 2 s after the last write.
    - In the other 3 the CBW logic ran start-up (TF power register 1, relay cycled back on), but the TF computer hadn't answered by 11:45:
      - 233187: lane A powered, CPU register 0, VNC down;
      - 234014: lane B powered, CPU down (lane A was already up);
      - 255094: lane A up, lane B powered with its ping timers running.
    - These now look like TF-computer problems, not DST. The CBW's own ping-fail reboot tasks are handling them.
  - Not touched:
    - the 10 sites that read as stuck but answer VNC on lane A (9 skipped live, plus 220237);
    - 244091 lane B (no lane-B logic; the user can switch its B2 relay back on if that lane has a computer);
    - the "running, relay on, no cycle" sites.

## Proposed approach
Phases are acceptance blocks; each is gated unless marked folded.

### Phase 0 — Logs and errors that survive a full run (gated)
Work:
1. Fix file logging for runs over 10 MB on Windows. Options include rotating by size per process, `--noreload` for scheduled runs, or a handler that tolerates the lock.
2. Store the run's full error list, not the 2,000-character truncation. Put every fleet-phase exception (OvrC, power-on summary) in its own field or file.

Acceptance:
- A test forces rollover while a second handle is open, and logging continues.
- A synthetic run with more than 2,000 characters of errors keeps the OvrC error.

### Phase 1 — OvrC fleet pass (gated)
Work:
The investigation is done; see E1's root cause. Work:
1. **Per-FX isolation.**
   - In `capture_fx_times`, catch errors per FX and return a result for every FX, either a reading or a reason (e.g. "not an OvrC customer").
   - After an error, navigate back to the customer list so the next FX starts clean.
   - `_dst_fleet_ovrc_times` records each reason on its own child job.
2. **Fail fast on an unknown FX.** "No results found" is a definite answer. Detect it instead of waiting the full 25 s search timeout.
3. **Optional pre-check:** before the pass, download OvrC's customer CSV once (as `ovrc_probe4.py` does). Then any FX that isn't a customer is marked without a search, and the list can be reported to IMS.
4. **Data (operator):**
   - FX1031 (200049) and FX1049 (220104) are not OvrC customers. Either add them in OvrC or accept "no OvrC" for these sites.
   - I-VBE-0019's `FX140` looks like a parse or typo next to `FX1410`; fix it in IMS.
5. Then the long-list leads that are still untested: session expiry over about 290 FX, and the per-FX WattBox time (about 2–3× the time per FX). The full re-run in step 1 of Acceptance measures both.

Acceptance:
- **Unit test:** `capture_fx_times(['FX_MISSING', 'FX_OK'])` with a fake client returns a reason for the first FX and a reading for the second.
- **Live:** `capture_fx_times(['FX1031', 'FX1051'])` returns FX1031 as "not an OvrC customer" within a few seconds, and FX1051 with a time. This is the exact repro that fails today.
- **Live, full list:** the standalone OvrC pass over run 389's 291 DE FX gets a time for every FX whose modem is up in PRTG at the time, or a per-FX reason. Record the elapsed time.
- 12 of the 13 OvrC-only sites from run 389 come out ok. 200049 (FX1031) comes out with the "not an OvrC customer" reason.

### Phase 2 — Audit scope: drop legacy and PRTG-paused sites before the run (gated)
2a. IMS side (IMS repo):
1. Add sensor `status` to `aseExternalSensor`.
2. Add an installation-level `prtg_monitoring` field: `active`, `paused` (every device sensor paused, or under a paused ancestor), or `unmonitored` (no PRTG device). Reuse `poleGroupAllSensorsPaused` and the CTE.
3. Add a token-scoped `POST /api/external/prtg-refresh` (new scope) that runs the tree sync and the sensor sync and returns a progress id. Add a `GET` to poll it.

2b. DST side:
1. Before the run, with the VPN off:
   - call `prtg-refresh` and wait, with a timeout;
   - then `sync_installations` with devices and sensors;
   - store sensor status and the installation's `prtg_monitoring` (migration).
2. Exclude two kinds of site before power-on:
   - **legacy:** no CBW or TF CPU host starts with `10.`;
   - **PRTG paused**;
   - **unmonitored** (D2, user 2026-10-30): no PRTG device at all, e.g. 233047. Skipped and hidden exactly like paused sites.
3. **Excluded sites are silently out of scope (user, 2026-10-30).** "Legacy and PRTG-paused sites should just be skipped in the audit. Don't even show them in the report."
   - No child row, no status, no count in the run or the review report.
   - They aren't counted as skipped-offline either.
   - Only the log records the exclusion: one line per site, plus a total.
4. If IMS or PRTG is unreachable, log a warning and use the last synced state. Never block the run.

Acceptance:
- IMS tests for the new fields and endpoint (scope enforced).
- DST tests for the exclusion rules, including 210071 kept and 233047 excluded.
- A live dry-run against the run-389 site set excludes about 38 legacy and about 32 paused sites, and the counts match `tmp/audit389/sites.csv`.
- None of the excluded sites appears in the run's children or in the review report. The log lists them.

### Phase 3 — Solar power-on (solar = has a Cerbo; spike first, gated)
Work:
0. **Solar = has a Cerbo.**
   - In DST, add an `is_solar` property (a `solar` device present) and use it everywhere instead of `power_type`.
   - Report the IMS records where `power_type` disagrees, so they can be corrected in IMS. Changing IMS data is an operator action, not done by DST.
1. **Done (read-only):** the schedule and logic are in E3.
2. **Spike: an on-demand power-on through the logic.** This writes to a live CBW, so it needs the user's go-ahead for each pole.
   - **First result (2026-10-30, user-approved, 220116):**
     - **Before.** At 06:15 the schedule set `register1`=1, but the M2 Vin > 13.3 V gate (Vin 13.2) blocked start-up. PRTG had the TF CPU down at 06:13:52.
     - **Write.** At 06:18:07 I wrote `state.json?register6=1&register7=1` (HTTP 200).
     - **The logic ran start-up as a power cycle.** It dropped relays 1 and 2 (TF A/B) for about 8 s at 06:18:24, brought them back at 06:18:41, then ran its step timers (≈290–356 s).
     - **Result.** PRTG had the TF CPU **up at 06:18:47**, 40 s after the write. VNC 5900 was open on `.140` and `.141`.
   - **Control: 220122** (no gate, normal schedule). `register1`=1 at 06:15 led to `register6`/`register7`, the same cycle, and the CPU up in PRTG at 06:17:42.
   - **What this shows.**
     - The TF needs the relay off→on cycle that the logic performs.
     - DST's direct `relayN=1` does not produce that cycle, and when the relays are already on it changes nothing.
     - The boot takes about 40 s–2.5 min after the logic starts.
   - **Measurement note.** ICMP ping to `.140` from the laptop over the VPN is filtered, so it always reads down. Use TCP 5900 or PRTG as the up signal, not ping.
   - **Off→on test (2026-10-30, user-run off, 220116): PASSED.** Log: `tmp/audit389/cbw_methodb_220116.log`, script: `cbw_methodb_test.py`.
     - **Off.** The user turned the site off. At 06:23:08 the off state was confirmed and held for 30 s: `register1`/`register6`/`register7`=0, TF relays 1 and 2 = 0, no timers running, VNC closed. Vin 13.2 V.
     - **Write.** At 06:23:08, `register1=1` and then `register6=1&register7=1`, both HTTP 200.
     - **The logic's start-up:**
       - +3 s: relay 1 (TF A) on;
       - +11 s: relay 2 (TF B) on after its delayed start;
       - +20 s: both dropped for the 8 s power cycle;
       - +28 s: both back on, step timers started.
     - **Result.** VNC 5900 on `.140` was **open at +42 s** (06:23:50).
   - **Still to test (acceptance):**
     - One weekend/evening run through the DST code path. There is no restore step: the user decided DST leaves sites on.
   - On one scheduled pole while it is off, write `state.json?register1=1`, the same thing "Photo Speed On" does.
   - Poll `state.json` and the CPU ping every 15 s until the CPU answers or 10 min pass. Record which registers, relays and inputs change, and when.
   - If the CPU does not come up, find the missing condition (for example "M2" needs Vin > 13.3 V, or the relays DST left on) and decide the minimal extra write.
   - After the test, put the pole back to its scheduled state.
3. **Replace relay forcing on scheduled solar CBWs** with a `power_on_via_logic`. E7 shows this isn't optional: forced relays block the next day's start-up.
   - DST must **never** write `relayN=1` on a scheduled solar CBW.
   - Proven start-up signals:
     - method B: the TF power registers → 1 (220116, 220084);
     - TF relay → 0 while `allTrufixPower`=1, which fires R1 (E7, 37 sites).
   - Lane B only gets the start-up write if the CBW has lane-B start-up tasks (244091 has none).
   - `power_on_via_logic` should:
   - read `io.data` and `schedTasks.data`, find the scheduled "on" target (I/O 10), and write it to 1;
   - poll for the CPU ping instead of a fixed 90 s settle (bounded by the logic's step timers, about 6–7 min);
   - record "battery low (Vin X V)" when the logic refuses.
   - Use it in both DST power-on and the rejection power-on.
   - CBWs with no schedule keep the current relay path.
4. **No restore after the audit (user, 2026-10-30).** DST leaves the site on after capture.
   - The CBW's own "Photo Speed Off" task (18:15, Monday–Friday) turns it off at the next scheduled time.
   - The low-voltage cut tasks still protect the battery in the meantime.
   - A site powered on at a weekend stays on until Monday 18:15 unless a low-voltage cut fires first.
   - **Register layout varies (found 2026-10-30, read-only, `cbw_sweep.json` and `cbw_*_io.data`).**
     - In `io.data`, `ioTypeID` 10 is a register, and its `state.json` key is `register{localIONum}`. A register with `localIONum` 0 isn't in `state.json`, so `state.json?…` can't write it.
     - So `power_on_via_logic` derives everything from the CBW's own config, never from fixed register numbers:
       - the schedule's "on" target (`schedTasks`: set I/O → 1);
       - the M2 step's TF power outputs (`condTasks`: operand 1 = that target, set actions);
       - each output's `state.json` key from `io.data`.
     - Of the 190 swept solar CBWs that were read:
       - **123**: scheduled, `register1`/`6`/`7` exposed (220116 / 220122 layout). Method B works.
       - **45**: no schedule. They keep the relay path, per step 3.
       - **22**: scheduled, 220084 layout. "All TruFix Power" (I/O 10) and the TF A/B power registers (I/O 19/26) have `localIONum` 0, so `state.json` only shows `register20/22/40/50`. Method B can't be written through `state.json`. This is **D6** (decided: C then B).
5. **Report the CBWs DST cannot manage:** the timeouts, plus any CBW that still rejects every password once the Phase 4 lockout handling is in (as of 2026-10-30, only 220061's manager password).
6. **Report the off-norm M2 gates (D5 Option A):** read-only `condTasks.data` with the manager password; one summary line per CBW whose M2 Vin gate differs from the norm or is inverted.

Acceptance:
- Spike: on the spike pole, from the off state, the write brings the TF CPU up (VNC L1 answers). **Met 2026-10-30 on 220116 (+42 s).**
- A small solar-only run outside schedule hours (evening or weekend) reaches a capture rate within about 10 points of grid sites.

### Phase 4 — CBW password lockout (gated: changes which sites power on; runs before Phase 3, which builds on its auth helper)
Work:
1. **Handle the lockout (root cause, E4).** In every CBW password loop, treat a 429 as "locked out": wait about 35 s and retry the **same** password, never advance to the next one. Applies to:
   - `services/cbw_relays.py`;
   - `services/cbw_firmware.py`;
   - `services/cbw_capture.py::_probe_cbw_auth`, where a 429 must not count as accepted;
   - the new Phase 3 `power_on_via_logic`.
2. **Avoid the lockout.** Remember the password that worked for each pole (e.g. in `cbw_password_hints.json`) and try it first. Reuse one authenticated session for reading state, writing registers and polling. That avoids both the 30 s wait and two extra failed logins per site.
3. Log a real 401-on-every-password (after any lockout wait) as `auth_failed`, separate from `locked_out` and offline, in power-on and in the run summary.
4. **D3 (user, 2026-10-30):** if a CBW really rejects every password (after any lockout wait), skip that site's CBW capture and power-on, but still attempt its VNC capture(s). The site carries the `auth_failed` CBW reason plus whatever VNC captured.
5. The fleet list needs no password changes: all 19 E4 poles accept an existing `CBW_PASSWORDS` entry.

Acceptance:
- Unit tests: a fake CBW that answers 401, 401, 429, then 200 makes the shared auth helper return password index 2. A real 401 on every password gives `auth_failed`, and a 429 that never clears gives `locked_out`. `_probe_cbw_auth` doesn't count a 429 as accepted.
- Live, read-only (GET `state.json` only, no writes): the helper authenticates all 19 E4 poles. A second pass uses the remembered password first and logs in with no 401 or 429.
- The device-write part (233006 powering on through `turn_all_relays_on` or `power_on_via_logic`) is covered by Phase 3's acceptance and Phase 5.

### Phase 4b — Review table fits the page, no sideways scroll (gated: user-visible)
Work:
1. Lay the review table out to the page width with a fixed layout (`table-layout: fixed` plus a `<colgroup>` of column widths), so no cell's content can widen the table. Remove the review table's `overflow-x` scroll.
2. Let the wide content wrap instead of stretching its column:
   - Status errors wrap inside the column (`overflow-wrap: anywhere`).
   - The three time columns break into date and time on two lines instead of `nowrap`.
   - Notes drops its `min-width` and takes the remaining width.
3. Use the wider `.container.wide` (1480 px) on the review page if the columns need it at 1920 px. Still no scroll at 1280 px.
4. Applies to every run's review page; nothing is specific to large runs.

Acceptance:
- On `/audits/review/15/` (run 389) and `/audits/review/14/` (small test) at 1280, 1600 and 1920 px browser widths, the page has no horizontal scrollbar (`scrollWidth == clientWidth` on the table wrapper and the document).
- The Notes cell is visible and clickable on every row, including the longest-error rows.
- Thumbnails, Assigned-to, Complete and Notes still work, including live updates.
- The user confirms it on the large run.

### Phase 5 — Full-fleet re-run (gated)
Re-run the full fleet. This is FAST_FLEET_DST_AUDIT Phase 5's acceptance plus this plan's:
- every DE site has an OvrC time or a per-FX reason;
- legacy, PRTG-paused and unmonitored sites are left out entirely (no child row, no report entry; the log lists them, per Phase 2b step 3);
- the solar capture rate is improved;
- the log is complete from start to end.

## Open decisions
- **D1 — 210017 / 210036. Decided 2026-10-30 by the user:** "The legacy rule applies to sites with an IP that does not start with 10.x. Since 10.21. starts with 10.x, those are not legacy sites and should be included in the audit."
  - So their IMS entries are wrong. Every device is listed on the modem's public IP: 210017 → `166.169.75.241`, 210036 → `63.46.113.59`. But the sites answer on `10.21.17.177` and `10.21.36.177`.
  - Fix: correct their CBW/CPU hosts in IMS to `10.21.x`. This is a data fix and needs no rule exception.
  - Check (read-only TCP, 2026-10-30, `tmp/audit389/legacy_10x_probe.json`): 40 run-389 sites have no `10.` CBW/CPU host in IMS. Only these 2 answer on `10.x` (CBW :80). The other 38 answer on neither CBW :80 nor CPU :5900, so they are really legacy.
- **D2 — Unmonitored installations. Decided 2026-10-30 by the user:** "yes, skip those without a device as well." Installations with no PRTG device (233047 and about 25 others) are skipped and hidden like paused ones (Phase 2b step 2).
- **D3 — CBW rejects the password. Decided 2026-10-30 by the user:** "if CBW rejects every password for real skip the CBW capture but attempt the VNC capture(s)." (Phase 4 step 4.)
- **D4 — Solar power-on when the CBW logic has a Vin gate. Decided 2026-10-30 by the user: Option B.**
  - DST writes `register1=1`, then writes the TF A/B power registers (`register6`/`register7`, I/O 15/16) to 1 itself when Vin is above a DST floor. That does the "M2 Photo Speed Power On" step without its gate.
  - The CBW's start-up, step and low-voltage-cut logic still runs.
  - No restore after capture (user, 2026-10-30). The site stays on until its own 18:15 weekday shutdown.
  - Rejected: A (accept the gate) and C (change fleet configs).
  - Why it's needed: the M2 gate differs per CBW, as observed 2026-10-30:
    - 220116: Vin > 13.3 V. Blocked at 13.2 V at 06:15.
    - 220122: no gate. It powered up at 06:15.
    - 220085: Vin < 12.8 V. Looks misconfigured.
  - **Correction (2026-10-30, E7).** Compare code 2 is "<" and code 5 is ">". This is proven by the low-voltage tasks: L1 "op2 11.8" cuts power *below* 11.8 V, and L3 is "op5 12.0".
    - 220116's M2 is code 5, so "Vin > 13.3" stands (re-checked in `cbw_220116_condTasks.data`).
    - The common gate is code 2 at 12.8, so it reads "Vin < 12.8" and never passes on a healthy battery: 86 of 96 CBWs read in E7, including 220084 and 220085. 2 have code 2 at 13.3 and 8 have no gate.
    - 220116's blocked 06:15 start may also have been E7: DST had left its relays on, so R1 couldn't fire.
    - The decision itself is unchanged: DST sets the TF power registers itself.
  - So `register1` alone is not a reliable on-demand signal.
  - Vin floor: **12.8 V** (user, 2026-10-30).
- **D6 — Solar power-on for the 22 scheduled CBWs whose logic registers aren't exposed in `state.json` (220084 layout). Decided 2026-10-30 by the user: "C then B".** Options: (A) keep forcing relays and report them; (B) don't write, and report "no on-demand power-on"; (C) a spike on one of them, with the user's approval for each write, to find another write path for unexposed registers, falling back to B. DST implements B now; C's read-only research comes first, then one approved write test.
  - **C, read-only research (2026-10-30, `tmp/audit389/d6_fetch.py`, `d6_custom_read.py`, output in `tmp/audit389/d6/`):**
    - The CBW control page (`ctrl-page.html`) writes I/O through `customState.json?<nameCamelCase>=<value>`, not `state.json`. That is its `loadStateJson(name + "=" + value)` call.
    - On 220084, `customState.json` lists `allTrufixPower`, `trufixAPower` and `trufixBPower`, the registers `state.json` hides. 220116 lists the same names.
    - So the candidate method B for the 22 is `customState.json?allTrufixPower=1`, then `customState.json?trufixAPower=1&trufixBPower=1`. The names come from each CBW's `io.data` `nameCamelCase`, because they vary per CBW.
    - The `.srv` endpoints in `setup.html` (`ioUpdate.srv` and the others) change configuration, which D4 rejected (option C), so they aren't used.
  - **C, write test (2026-10-30; the user turned 220084 and 220116 off; `tmp/audit389/d6_write_test.py`, `d6_write_test.log`):**
    - **220084 (hidden-register layout): the write path works.**
      - Off was confirmed at 10:17:04 and held 30 s: `allTrufixPower`, `trufixAPower` and `trufixBPower` 0, TF relays 0, VNC closed, Vin 13.2 V.
      - At about 10:17:34 the script sent `customState.json?allTrufixPower=1`, then `customState.json?trufixAPower=1&trufixBPower=1`. Both requests returned.
      - At about 10:25 a read-only check showed all three at 1, TF relays 1 and 2 on, and **VNC 5900 open on `.140`**.
      - Gap: right after the writes, the script crashed on a logging bug (a duplicate `vin` keyword). So the HTTP codes and the exact time to CPU-up weren't recorded; the CPU was up within about 8 minutes.
    - **220116 (standard layout): not tested.** It read on throughout (all three at 1, VNC open, Vin 13.9–14.1 V), so the off condition never held and nothing was written. I stopped the run at about 10:26.
- **D5 — Report the inconsistent M2 battery gates. Decided 2026-10-30 by the user: Option A.** DST reads each solar CBW's `condTasks.data` during the solar pass and lists the off-norm "M2 Photo Speed Power On" gates in the run summary for cleanup (Phase 3 step 6). Examples: 220116 needs "> 13.3 V" (code 5); 220122 has none; 220084, 220085 and 86 of the 96 E7 CBWs have "< 12.8 V" (code 2, inverted). Operators confirmed 2026-10-30: code 2 is "<" and code 5 is ">"; see D4.

## Status
in progress — 12/13 (Phases 0, 1, 4, 3, 4b and 2 accepted; D1–D6 decided; D1 data fix done). Phases 3, 4b and 2 were accepted 2026-10-30 (user: "acceptance passed, redeploy"), on the mixed acceptance list (LTI 220116, 220084, 233009, 220008, 210001; DE 266134, 255037, 244123, 266110, 255043). Only Phase 5, the full-fleet re-run, remains. The Malibu (700001–700020) exclusion still needs the user's confirmation before it.
- **Sequencing (user, 2026-10-30).** Investigate every root cause first, then implement the whole plan; every root cause is now found. "Start implementation" (user, 2026-10-30). Phases are still accepted one at a time, in the checklist's order.
- **Root causes:**
  - E1 (OvrC): found.
  - E3 (solar): found, and method B proven on 220116.
  - E4 (passwords): found. A 429 lockout after two failed logins; all 19 accept index 2.
  - E6 (review table width): found. Automatic table layout plus no-wrap time cells and long status errors.
  - E2/E5 (scope): no investigation needed. The user decided to skip legacy and PRTG-paused sites and leave them out of the report (2026-10-30).
  - E7 (run 1220 solar misses): found and proven by intervention. DST's evening relay forcing blocks the next morning's CBW start-up. The stuck sites were recovered 2026-10-30.
- **Decisions:** D1–D6 are all decided (2026-10-30), and D1's IMS data fix is done.
- **Hold (E7): lifted 2026-10-30.** Phase 3 is accepted, so scheduled solar CBWs are no longer relay-forced. The first evening run should still be checked the next morning for stuck sites.

## Checklist
Execution order. Phase 4 runs before 3 because 3's `power_on_via_logic` uses 4's auth helper. Phase 2 runs late because it needs the IMS-repo change and D1/D2.
- [x] Phase 0: logs and errors survive a full run (accepted 2026-10-30)
  - `config/logging.py` `LockTolerantRotatingFileHandler` (with `delay=True`): moves the base file aside first, leaves the backups alone if the rename is refused, and retries after 60 s.
  - The DST parent keeps the full `error_message` and writes `media/audits/<job>/run_errors.txt`.
  - `audits/test_run_logging.py` passes 4/4, including a real second open handle on Windows. The stock handler fails the same scenario: 17/20 records lost and backup `.1` slid to `.3`, matching run 389's frozen 10 MB `dst.log` with no backups. The full suite passes 242/242.
  - Not committed yet. The running server picks the change up on its next restart.
- [x] Phase 1: OvrC per-FX isolation and fail-fast (accepted, including the live FX1031/FX1051 repro). Accepted 2026-10-30.
  - `services/ovrc_capture.py`:
    - `capture_fx_times` returns one result per FX, a reading or `OvrcTimeResult.error`;
    - `OvrcClient.recover()` returns to the customer list, or restarts the session;
    - a lost session marks the remaining FX instead of discarding the readings;
    - "No results found." raises `OvrcCustomerNotFound` once it has held 1.5 s (at least 2.5 s after typing).
  - `audits/runner.py`: both callers attach each FX's reason to its own site.
  - Unit tests: `audits/test_ovrc_isolation.py` passes 7/7; the full suite passes 249/249.
  - **Live repro met:** FX1031 → "FX1031 is not an OvrC customer (search: No results found)" at +3.0 s (was 36.6 s, aborting the pass). FX1051 read right after it, WattBox included. Whole pass 21.3 s.
  - **Full-list pass met:** 283 unique DE FX of run 389's children (`tmp/audit389/de_fx_389.json`, `ovrc_phase1_full.log`); E1's 291 came from a different lookup. In 3,062 s (51 min, about 10.8 s per FX with the WattBox), one session ran the whole list with no session loss. 281 FX were read; FX1031 and FX1049 got "not an OvrC customer".
  - **13 OvrC-only sites met:** 12 read, including 266165, whose modem was down in run 389. 200049 (FX1031) has the "not an OvrC customer" reason.
  - **Found and fixed during acceptance: wrong-site reading.** The search matched a substring, so FX140 (I-VBE-0019) opened **FX1400**'s dashboard and reported its time (`ovrc_phase1/ovrc_fx140.png`).
    - Rows and links now have to match the whole FX id.
    - A search that lists only longer ids raises `OvrcCustomerNotFound` ("search: only FX1400, …").
    - Covered by 3 new tests in `WholeFxIdTests`; the full suite passes 252/252.
    - Live re-check: FX140 → "not an OvrC customer (search: only FX1400 … FX1409)". FX1400 and FX1410 read normally, and FX1031/FX1051 behave as before.
  - **WattBox time misses: 2 of 283, transient.** FX1359 ("Your device is set to time zone…" prompt) and FX1238 got their dashboard time but not their WattBox time. Each carries a per-FX `wattbox_error`, and both read cleanly on a live retry. No change made.
  - Step 3 (CSV pre-check) is not done; it's optional. Step 4 is operator data: FX1031 and FX1049 aren't OvrC customers, and I-VBE-0019's `FX140` should be fixed in IMS (likely `FX1410`).
- [x] Phase 4: CBW lockout-aware auth helper (accepted, unit tests plus a read-only live pass on the 19 E4 poles). Accepted 2026-10-30.
  - New `services/cbw_auth.py::cbw_login`:
    - tries the remembered password number first (`cbw_password_hints.json`, keyed per host and path; `setup.html` keeps the old bare-host key);
    - on a 429, waits 35 s and retries the **same** password, up to 2 waits;
    - raises `CbwAuthError` with reason `auth_failed` (every password got a real 401) or `locked_out` (the 429 never cleared), or `CbwUnreachable` (no HTTP answer);
    - returns the authenticated session and the first response, so callers reuse them.
  - Callers:
    - `cbw_relays.turn_all_relays_on`: one login, reads state from the login response, and writes the relays on the same session;
    - `cbw_firmware.read_cbw_firmware`: logs in on `overview.json` itself (manager password), with a new `locked_out` status;
    - `cbw_capture._probe_cbw_auth`: now one lockout-aware login on `setup.html` that returns the accepted password number. A 429 is never counted as accepted, and an all-401 CBW raises before Selenium starts.
    - Phase 3's `power_on_via_logic` will use `cbw_login` too.
  - D3 (`audits/runner.py`):
    - `_dst_power_on_pass` records a `CbwAuthError` in `run.cbw_unmanaged` instead of retrying it or marking the site offline;
    - the site's capture gets `cbw_skip`, so `_run_pole_bundle_parallel` runs VNC only and records "CBW: … capture skipped";
    - the run errors get "power-on <site>: CBW auth_failed … (CBW skipped, VNC tried)".
  - Tests:
    - `audits/test_cbw_auth.py` covers the fake CBW 401, 401, 429, 200 → password 3 (index 2) with one wait, plus the hint first, `auth_failed`, `locked_out`, unreachable, the probe not counting a 429, relays on one session, and the VNC-only bundle;
    - `test_network_phases` gets a D3 fleet-run test;
    - the firmware and password-order tests were adapted;
    - the full suite passes 266/266.
  - **Live, read-only** (GET `state.json` only; `tmp/audit389/phase4_live.py`, `phase4_live.log`; temporary hint file):
    - pass 1: all 19 E4 poles were ok on password #3, each 401, 401, 429, then 200 after one wait (75 s with 10 in parallel);
    - pass 2: all 19 logged in with a single 200 on the remembered password and no 401 or 429 (1.0 s).
- [x] Phase 3: solar power-on via logic, method B (accepted). Accepted 2026-10-30 (user: "acceptance passed").
  - New `services/cbw_logic.py`:
    - `parse_logic` reads each CBW's `io.data`, `schedTasks.data` and `condTasks.data` and derives everything from them: the schedule's "on" register, the lane power registers (from M2), each lane's start-up relay, the Vin input and the M2 gate.
    - `power_on_cbw`:
      - logs in on `setup.html` (manager password, via `cbw_login`);
      - a scheduled CBW gets method B through `customState.json`: the on-register = 1, then lane power = 1 only for lanes that have start-up logic;
      - it never writes `relayN=1` on a scheduled CBW;
      - below the 12.8 V floor it writes nothing and returns `battery_low`;
      - an unscheduled CBW, or one whose schedule switches the relay itself (233009 layout), keeps the relay path (`cbw_relays.relays_on`).
  - `audits/runner.py`:
    - `_power_on_lti_site` uses `power_on_cbw`. A grid site with no manager password still gets relays; a Cerbo site never does (D3 instead).
    - Lanes started through the logic go into `run.boot_hosts`, and `_dst_wait_for_boot` polls their VNC (`DST_SOLAR_BOOT_TIMEOUT_SECONDS`, 420) instead of the fixed settle.
    - `_dst_cbw_report` adds "CBW report:" lines to the run errors (steps 0, 5 and 6): battery low, unmanaged, offline, inverted or missing M2 gates, lanes with no start-up logic, Cerbo sites on the relay path, and IMS `power_type` disagreeing with the Cerbo.
    - The rejection power-on and the list page's relays button use `power_on_cbw` too.
    - `Installation.is_solar` (Cerbo device) is new.
  - Tests:
    - `audits/test_cbw_logic.py`: 22 tests on real fixtures (220116, 220084, 220122, 220080, 233009 in `audits/fixtures/cbw/`);
    - the full suite passes.
  - **Read-only fleet check** (`tmp/audit389/p3_parse_fleet.py`, `p3_parse_fleet.json`): of 262 Cerbo CBWs, 220 answered (41 unreachable, 1 manager-password reject).
    - 179 are scheduled. 176 parse to lanes with start-up logic (126 lane A only plus B power without start-up; 50 with both lanes). 3 are the relay-schedule layout (233009, 233152, 233213).
    - Every derived name exists in `customState.json`.
    - M2 gates: 121 "< 12.8", 46 none, 9 "> 13.3", 2 "< 13.3", 1 "> 12.9".
  - **Accepted 2026-10-30** by the user's acceptance run. Still to watch: no stuck solar sites the morning after the first evening run (Phase 5).
- [x] Phase 4b: review table fits the page (accepted, including the user's confirmation). Accepted 2026-10-30 (user: "acceptance passed").
  - `templates/audits/review.html` and `static/app.css`:
    - fixed table layout with a `<colgroup>`; Notes takes the remaining width;
    - no `overflow-x` wrapper;
    - date and time on two lines (the `date_then_time` filter);
    - errors wrap; header words don't break.
  - **Measured** (`tmp/p4b/render_review.py`; pages rendered offline from the real data, headless Chrome):
    - run 389 (`/review/15/`, 813 cameras) and `/review/14/` (12) at 1280, 1600 and 1920 px: `scrollWidth == clientWidth` for the document and the table on every one;
    - the Notes cell is fully on-page on all 813 rows (198 px wide at 1280, 265 px at 1600+).
  - The full suite passes; the review test was updated for the two-line time.
  - Still needed: the live-update and edit check on the real page, and the user's confirmation. The shared review deploy is DST_AUDIT_SHARED_REVIEW's step and isn't done here.
- [x] D1 decided (user, 2026-10-30): 210017 / 210036 are not legacy.
- [x] D1 data: correct the 210017 / 210036 CBW/CPU hosts in IMS to `10.21.x`. Done 2026-10-30 (user: "Make the IMS host fix").
  - Cause: the IMS hosts are a stale copy of PRTG. `enclosures_sync_build_poles.go:63` upserts `host` from PRTG, and PRTG already had `10.21.x` for the same objids (5473–5481, 4963–4973). IMS still had the modem's public IP.
  - Fix (`tmp/audit389/d1_ims_fix.py`): one transaction on the IMS **DEV** database (the profile the local IMS, and so DST, reads). It set the 8 `enclosure_devices` rows (modem `.1`, CBW `.177`, TF CPU `.140`, camera `.150`) to PRTG's hosts. Each update was guarded on objid and the old host, and the rows were read back.
  - The next PRTG sync writes the same values, so nothing reverts. The production IMS database wasn't checked: there are no production credentials on this machine.
  - DST picks the change up on its next IMS installation refresh, which has to happen before Phase 2.
  - Side finding (read-only, `d1_ims_vs_prtg.json`): 18 other IMS device hosts on 17 sites differ from PRTG, all `10.x` on both sides, so the legacy rule isn't affected. Examples: the 233085 / 233086 TF CPU is `.150` in IMS but `.140` in PRTG; 266132's camera is `10.6132.x`. Not changed.
- [x] D2 decided (user, 2026-10-30): unmonitored installations are skipped and hidden.
- [x] Phase 2: audit scope, IMS plus DST (accepted). Accepted 2026-10-30 (user: "acceptance passed").
  - **IMS (uncommitted; other threads' unrelated changes in that repo were left alone):**
    - sensor `status` (from `last_status`) in the ASE external API;
    - `prtg_monitoring` (active / paused / unmonitored, from `asePRTGMonitoring`) with `?include=devices`;
    - new scope `prtg:refresh` (a checkbox on the token form) and `POST`/`GET /api/external/prtg-refresh` (`external_prtg_refresh.go`): tree sync, then the enclosure build, then the sensor sync, one at a time, with a progress id;
    - `go vet` is clean and all tests pass, with 4 new ones (`external_prtg_refresh_test.go`).
  - **DST:**
    - `Installation.prtg_monitoring` and `InstallationSensor.status`; migration `cameras/0008` (applied to the laptop DB 2026-10-30);
    - `ims_sync` stores both;
    - `ims_client.refresh_prtg` starts the refresh and polls it;
    - `Installation.is_legacy`: has CBW/CPU devices and none on `10.`;
    - `runner._dst_refresh_scope` (before site selection; any failure is a warning and the last synced state is used; `DST_PRE_AUDIT_IMS_REFRESH`);
    - `_dst_drop_out_of_scope`: paused, unmonitored, and legacy for LTI only, dropped before any child row, logged per site plus a total;
    - tests: `audits/test_audit_scope.py` plus 2 run-level tests in `test_network_phases`; the full suite passes 303/303.
  - **Dry run (read-only, `tmp/audit389/p2_dry_run.py`):** run 389's 814 sites, with `prtg_monitoring` and the legacy hosts computed from the IMS DEV DB by the same join and rule:
    - out: 37 legacy, 57 PRTG paused (44 LTI, 13 DE) and 27 with no PRTG device;
    - kept: 693;
    - 210071, 210017 and 210036 are kept; 233047 is out.
    - Paused covers all 35 paused sites in `sites.csv`, plus **Malibu 700001–700020** (every sensor "Paused (paused by parent)"; run 389 captured 15 of them), 220153 and I-VBE-0010. **The Malibu exclusion needs the user's confirmation.**
  - **Acceptance still needed:**
    - IMS rebuilt and restarted with this code, and a `prtg:refresh` token in DST's `IMS_API_TOKEN`;
    - `manage.py migrate`;
    - then a live dry run whose counts match the above, and a run whose children and review leave the excluded sites out.
- [ ] Phase 5: full-fleet re-run (accepted)
- [x] D3 decided (user, 2026-10-30): skip the CBW, attempt VNC.
- [x] D5 decided (user, 2026-10-30): Option A, report the off-norm M2 gates.
- [x] D6 decided (user, 2026-10-30): C then B. The spike powered 220084 on through `customState.json`.
