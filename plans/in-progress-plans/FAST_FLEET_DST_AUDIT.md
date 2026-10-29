# Initial plan — Full-fleet DST audit in under 8 hours

Discussed: 2026-10-28, during run 196, the first full-fleet run (814 sites, started 21:40).
- The user asked: "If they are offline then we shouldn't even try to reach it — based on the turn on results and then sensor status from IMS/PRTG … anyway we could add concurrency to the LTI portion at least to speed that half up?"
- Then: "Draft a plan, then look into how we can speed up DE sites as well. 40+ hours simply will not do. Ideally <8 but really it's as fast as we can get it as possible."

Stage: in progress (promoted 2026-10-29: the user said "start implementation")
Related:
- plans/in-progress-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md (owns the fleet run's order: LTI on the VPN → release → DE direct. This plan speeds up each half and keeps that order unchanged)
- plans/in-progress-plans/DST_AUDIT_SHARED_REVIEW.md (the review consumes per-site results; "skipped" is a new site outcome it has to show)

**Do not implement while a DST run is in progress.** The laptop server (`runserver`, :8050) reloads on any `.py` change, and the reload kills the running job thread.

## Problem
Run 196 is on pace for a day or more. That doesn't fit overnight.

**Evidence: run 196** (`logs/dst.log`, `AuditJob` 196 and its children; read-only, 2026-10-28 22:20):
- **Sites:** 814 = 530 LTI + 284 DE.
- **LTI power-on:** 373 ok, **157 failed** (the CBW didn't answer on the VPN). It took 14 min with 3 workers. Each failed pole costs ~15 s.
- **LTI capture still tries every site, including the 157.**
  - An unreachable site burns **~165–185 s** before failing: `services/cbw_capture.py:99` (45 s page load) × password retries, plus VNC (`services/vnc_capture.py:43`).
  - After 26 min, 10 sites were done: 1 ok and 9 failed. All 9 failures were sites whose power-on had already failed.
- **LTI sites are captured one at a time** (`audits/runner.py` `_dst_capture_half`). Only the 3 steps inside a site run in parallel (`AUDIT_STEP_CONCURRENT=3`).
- **Online LTI site times vary widely:** 18 s (220008, 220105) up to 282 s (220054: VNC lanes timed out on a working site, run 188).
- **The parent's `capture 1/814` counts successes over the whole fleet,** so during the LTI half it reads as "stuck".

**Evidence: DE** (run 188 log, 3 sites, 6 lanes):
- **An online lane takes ~52 s** (from the `[TV] saved … elapsed=` lines):
  - fixed 10 s sleep after launching TeamViewer;
  - ~3 s activate, plus a stability check;
  - **both** camera passwords typed, each followed by a fixed 7 s sleep (~32 s together), even when the first one unlocks the screen;
  - ~5 s capture.
- **A lane stuck on the reconnect overlay took 111 s, then failed** (244127 L2).
- **The DE totals are ~6× lower than I first told the user.** The stored child durations (190–400 s per site) include the later fleet OvrC pass, because `_dst_fleet_ovrc_times` bumps `finished_at` (`audits/runner.py` ~line 940). The log shows ~105 s for a 2-lane site.
- **The fleet has 284 DE sites but only 231 TeamViewer lanes:**
  - **119 sites have no TeamViewer mapping** and fail instantly ("No TeamViewer ID mapped");
  - 111 have 1 lane, 47 have 2, 5 have 3, 1 has 5, 1 has 6.
- **An offline TeamViewer partner costs up to 2 passwords × (10 s + 60 s wait) ≈ 140 s per lane** (`services/teamviewer_capture.py:638-675`).
- **The DE screenshot must be the logged-in desktop.** It shows the DragonCam overlay `Date/Time` and the GNOME top-bar clock (`media/audits/188/255039/de_l1.png`). The lock screen alone wouldn't show DragonCam.

**Evidence: PRTG/IMS status as an offline signal.** The ping sensors copied from IMS (`cameras.InstallationSensor.last_value`) were checked against tonight's LTI power-on results:

| Tonight's power-on | PRTG copy: up | PRTG copy: down | No data |
|---|---|---|---|
| Failed (157) | 70 | 69 | 18 |
| Worked (373) | 354 | 8 | 11 |

The copy only updates when IMS syncs, so it's stale. **It's too wrong to skip sites on.** Tonight's power-on result, minutes old and over the same network path, is the reliable signal.

## Research
- **S (formal):** Amdahl's law. The fleet time is bounded by the serial part.
  - **LTI** is network I/O end to end (headless Chrome + VNC), so it parallelises well.
  - **DE** has one serial resource, the foreground keyboard/mouse used to type the remote login. Pipelining lets the waits (connect, reconnect, unlock) of one lane overlap the typing of another. That removes everything except the typing from the serial path.
- **T (third-party practice):**
  - Selenium/WebDriver fleets run N headless browsers at once, bounded by RAM (~150–300 MB each).
  - TeamViewer clients can hold several outgoing sessions at once (tabs), but the **licence limits concurrent sessions** (channels). The laptop's licence type code is `10000` (`HKLM\SOFTWARE\TeamViewer`, v15.83.3), and how many channels it allows is **unverified**.
- **M (our own runs):**
  - The run 196 and run 188 measurements above are the baseline.
  - Run 196, once finished, gives the fleet-scale numbers that only it can: the DE OvrC power-on time for ~165 FX, the offline-lane share, and the OvrC times pass.
  - The DE parallel-session idea has **no prior run** to back it. Phase 4 starts with a spike.

## Proposed approach
Two levers, applied in this order:
1. **Don't try what is known dead.**
2. **Remove fixed waits, then add parallelism where the resource allows it.**

The LTI → release → DE order from UNATTENDED_NIGHTLY_DST_AUDIT doesn't change.

**Projected run after Phases 1–3:**

| Step | Today (run 196 pace) | After |
|---|---|---|
| LTI power-on (530) | 14 min, 3 workers | ~4 min, 10 workers |
| Boot wait | 90 s | 90 s |
| LTI capture | ~15–20 h (serial, offline sites tried) | **~0.5–1.6 h** (157 skipped; 373 × 20–60 s ÷ 4) |
| DE OvrC power-on | (measure in run 196) | same |
| DE capture (231 lanes) | ~3.5–6 h (52 s/lane + 140 s per offline lane) | **~1.5–2 h** (~25 s/lane; offline fails in ~20 s) |
| OvrC times pass | 22.6 s for 3 FX (measure fleet) | same |
| **Total** | **~20+ h** | **~3–4.5 h** |

Phase 4 (parallel DE sessions) is only needed if Phase 3's measured DE time misses the budget.

## Issue digest
- **F1 — Offline sites are captured anyway.**
  - 157 LTI sites at ~3 min each, ~7.5 h of timeouts.
  - Fix: skip capture for any site whose power-on failed. Record it as the outcome **skipped (offline: power-on failed)**, not failed.
  - For DE, skip an FX that OvrC reports as offline or returns no result for, and skip the 0-lane sites as **skipped (no TeamViewer ID)**. Phase 1.
- **F2 — LTI sites run one at a time.**
  - Fix: run sites in a pool, `DST_LTI_SITE_CONCURRENT` (default 4), and raise power-on workers to 10. Phase 2.
- **F3 — The progress counter misleads.**
  - Fix: show it per half, e.g. `LTI 120/530 · 98 ok · 4 failed · 18 skipped`. Phase 2.
- **F4 — DE lanes wait on fixed sleeps and always type both passwords.** Phase 3. Fixes:
  - poll for the session window instead of sleeping 10 s;
  - after each password, detect the unlocked desktop and stop;
  - try the password that last worked for that TeamViewer ID first.
  - Typing the second password into an already unlocked desktop also risks keystrokes landing in DragonCam.
- **F5 — An offline TeamViewer partner costs ~140 s.**
  - Fix: detect TeamViewer's "partner not ready / not connected" or "wrong password" dialog and fail fast. Don't retry the next connection password when the partner is offline. Phase 3.
- **F6 — 119 of 284 DE sites have no TeamViewer mapping.**
  - This is a data gap, not speed. Phase 1 reports them as skipped.
  - Filling the gap means updating `DragonEye Teamviewer IDs.csv`.
  - **2026-10-29:** the user supplied `Downloads\DRTV-keeper7_30_26.csv`: 433 lines, 406 FX/lane keys, against 235 in the repo CSV.
    - **Coverage:** with today's parser it covers **96 of the 119** unmapped sites. Parser fixes would cover **100**:
      - bare `1409L`/`1409R` labels (FX1409, site 266145);
      - an FX at the end of the label (`Wauchula ES NB FX1128` / `SB FX1126`, sites 255031/255032);
      - lane IDs in extra columns (`FX1184 VBE0010,,L1 TV ID,…,L2TVID,…`, site I-VBE-0010);
      - IDs written with spaces (`FX1219 L1,117 259 899`).
    - **Conflict:** `FX1070 L3` is `1108266678` in the repo CSV and `630799567` in the keeper file.
    - **Still unmapped (19):**
      - 200049 (FX1031);
      - 244196–244199 (FX1103/1105/1109/1110);
      - 266001 (FX1302) and 266005 (FX1296);
      - 266110/131/132 (FX1435/1436/1437);
      - 266149–266151 (FX1429/1427/1428);
      - 266165–266167 (FX1438/1439/1440);
      - 266173–266175 (FX1441/1442/1425).
    - **Column 3 holds what look like per-device passwords** (7–8 characters; 60 blank). Not copied anywhere. Phase 3 could use them to type the right password first, if the user approves storing them.
    - **Importing it changes data only, no code.** The UI upload *replaces* every mapping, so the import should merge instead.
  - **Resolved 2026-10-29** (user: "Import now, for the others don't include the TV in the audit but do include the ovrc … THe pw are already saved in the .env").
    - **The import was add-only** (`tmp/import/merge_tv_ids.py`): it added 176 rows for 129 FX, and existing rows were left untouched.
    - **Lane fixes where the stored IDs agreed with the file:**
      - FX1131 and FX1297 had one unlabelled row (the R camera); each is now L1 + L2.
      - FX1211 gained L2 and FX1410 gained L1.
    - **The table has 424 rows.** DE sites with no TeamViewer ID went from **119 to 13**, and the fleet now has **384** TeamViewer lanes.
    - **Still unmapped (13):**
      - 200049 (FX1031);
      - 266110/131/132 (FX1435/1436/1437);
      - 266149–266151 (FX1429/1427/1428);
      - 266165–266167 (FX1438/1439/1440);
      - 266173–266175 (FX1441/1442/1425).

      **Per the user, these get the OvrC time only, with no TeamViewer** (Phase 1 item 3).
    - **FX1070 L3:** the stored `1108266678` is kept and the file's `630799567` is not applied. Settle it on the first run: if the stored ID doesn't connect, try the other.
    - **Column 3 passwords (matched in memory, values never printed):**
      - 216 devices use `.env` camera password #1;
      - 155 use #2;
      - 60 are blank;
      - 2 match neither.

      Phase 3 stores only the **index** per TeamViewer ID and types that password first.
- **F7 — Online LTI sites sometimes take ~5 min** (220054 VNC lane timeouts on a working site).
  - Fix: find which timeout fires (CBW 45 s page load × retries, or VNC 15 s × ?) and cap the per-site budget. Phase 2.
- **F8 — The DE child `finished_at` includes the OvrC pass.** That inflates site durations and misled the first estimate.
  - Fix: keep the capture finish time separate. Phase 2, folded in with the progress fix.
- **F9 — Carry-over from run 188: the DE OvrC power-on raised an exception with an empty message** (`power-on DE fleet: Message: `).
  - It's the same code path at fleet scale. Watch run 196's result; if it repeats, fix it in Phase 1, since a failed fleet power-on would make F1 skip every DE site.

## Execution phases

### Phase 1 — Skip what is known offline (gated)
Work:
1. **Have the LTI power-on record its result per site;** the DE OvrC power-on already does, per FX.
2. **In `_dst_capture_half`, skip a site whose power-on failed.**
   - Create its child job anyway with status **skipped** and the reason, so the review shows every site.
   - This adds a new `AuditJob.Status` value and a migration.
3. **For DE:**
   - sites with 0 TeamViewer lanes get **no TeamViewer step but still get the OvrC time** (user, 2026-10-29). They count as **ok (OvrC only)**, not failed;
   - skip FX with no or failed OvrC result ("skipped: OvrC offline").
   - **If the whole OvrC power-on call raised an exception, skip no DE sites; try them all and warn (F9).** A broken OvrC login must not wipe out the DE half.
4. **The run summary counts skipped sites separately from failed ones.**

Acceptance:
- Unit tests:
  - a failed power-on means skip with no capture call;
  - an OvrC exception means no DE skips;
  - a 0-lane DE site is skipped.
  - The full suite stays green.
- Live run on 4 LTI sites: 2 known offline (220039, 220040) and 2 online (220008, 220105).
  - The offline pair is skipped within seconds; the online pair is captured as before.

Result (2026-10-29):
- **Implemented.**
  - New `AuditJob.Status.SKIPPED`; migration `0012` has no SQL, so it's a no-op on SQLite and Postgres.
  - The runner skips LTI sites whose power-on failed twice, and DE sites whose OvrC DCAM failed. A failed OvrC login skips nothing.
  - `ensure_dcam_on_for_fxes(continue_on_error=True)`, used by DST only, lets one bad FX fail without stopping the rest.
  - OvrC-only path for DE sites with no TeamViewer ID.
  - Publish treats SKIPPED as finished. The review counts it as "skipped offline", not failed.
  - A cancel closes children that are still waiting.
- **Change from the plan, found in acceptance:** the LTI power-on retries failed sites once before marking them offline.
  - In job 211, 220105 timed out on all 3 relay attempts at 08:24:51, yet its CBW answered on port 80 in 0.15 s a minute later. One failed pass is not proof.
  - The retry costs ~15 s for truly dead sites.
- **Tests:** 146 OK, 8 new:
  - skip, retry, DE OvrC failure → skip, OvrC login failure → no skip;
  - OvrC-only ok or failed, continue-on-error;
  - review "skipped" count, publish of a SKIPPED child.
- **Live, job 216** (VPN on, no publish), 4 sites in 69 s:
  - 220039 and 220040 failed both passes and were skipped (the retry took 16 s);
  - 220008 ok in 16 s with 3 shots;
  - 220105 ok in 16 s: CBW shot ok, VNC L1/L2 timed out. That matches run 196 (1 shot), so it's not a regression; it's F7, Phase 2.
- **Not yet live-tested: the DE parts** (OvrC per-FX failure → skip; OvrC-only sites). They need the VPN off and are folded into Phase 3's live DE acceptance.

### Phase 2 — LTI in parallel and honest progress (gated)
Work:
1. **Run the LTI capture in a site pool** (`DST_LTI_SITE_CONCURRENT`, default 4).
   - Children are created and finished per site exactly as now.
   - Cancel still stops everything.
   - The parent progress reports the half-level counts (F3).
2. **Raise power-on workers** to `DST_POWER_ON_CONCURRENT` (default 10).
3. **F7:** log which timeout fires per lane on slow online sites, and cap a site at a total budget (e.g. 120 s) so one bad lane can't stall a worker for 5 min.
4. **F8:** stop the OvrC pass from moving the capture's `finished_at`.

Acceptance:
- Unit tests for the pool (all sites captured once, cancel mid-pool, counts).
- Live: 20 online LTI sites on the VPN, timed. Wall time ≤ serial time ÷ 3.
  - Laptop RAM stays under ~80 % during the run.
  - No CBW or VNC errors that the serial run doesn't show on the same sites.

### Phase 3 — Faster DE lanes (gated)
Work:
1. **Replace the fixed 10 s launch sleep** with polling for the session window, every 0.5 s.
2. **After each camera password,** grab the session and check whether the lock screen is gone, with the same image heuristic style as `_looks_like_reconnect_dialog`. If it's gone, stop typing.
   - Remember the working password per TeamViewer ID (a small table or JSON) and try it first next time.
3. **Shorten the post-password 7 s and 3 s sleeps** to "poll until the desktop is stable, up to N s".
4. **F5:** detect TeamViewer's "partner not ready / not connected to the router" or "wrong password" dialog by window title or text, and fail fast.
   - Offline partner: don't retry the next connection password.
   - Wrong password: try the next one.
5. **Cap the reconnect-overlay wait per lane** (today 55 s + 40 s + 5 retries).

Acceptance:
- Live on the 3 test DE sites (255039, 266007, 244127) plus 1 known-offline TeamViewer ID.
  - Online lanes take **≤ 25 s each** (log `elapsed=`).
  - The offline lane fails in **≤ 30 s**.
  - The screenshots still show the logged-in DragonCam desktop with its Date/Time overlay.
  - No keystrokes land in DragonCam.
- Unit tests for the password order and the fail-fast classification.

### Phase 4 — Parallel DE sessions (gated, conditional)
**Only if Phase 3's projected DE time across 231 lanes is still over budget.**

Starts with a **spike**, a throwaway that answers 3 questions:
- (a) How many concurrent outgoing sessions does this TeamViewer licence allow?
- (b) Does `PrintWindow` capture a session window that is not in the foreground?
- (c) Can 2 sessions connect at once while the keyboard step is held by a lock?

If all 3 pass: pipeline the lanes. Connects and waits run in parallel; only focus + typing + capture holds a single "desktop" lock.

Acceptance:
- Live run on 6 DE lanes with 2 sessions: wall time ≤ 0.6 × the Phase 3 serial time.
- Every screenshot shows the right site (the window title matches the TeamViewer ID; no cross-site capture, the FX1260/FX1361 bug).

### Phase 5 — Full-fleet timed run (gated)
Work: a full scheduled run of all eligible sites, per UNATTENDED_NIGHTLY_DST_AUDIT.

Acceptance:
- **The total is under 8 h.**
- Every site has an outcome: ok, failed or skipped, each with a reason.
- Auto-publish lands in the review dropdown.
- The phase timings are recorded in this plan's Verification section.

## Status
in progress — Phase 1 implemented and accepted for LTI on 2026-10-29 (DE live checks are carried into Phase 3). Next: Phase 2.

## Checklist
- [x] User decision F6: keeper CSV merge-imported 2026-10-29 (119 → 13 unmapped); the 13 get OvrC only; passwords stay in `.env`, and only a per-device index will be stored.
- [x] Run 196 cancelled (2026-10-28 22:30, by the user from the page). It never reached DE, so there is no DE or OvrC fleet baseline; Phase 3/5 measure their own.
- [x] Phase 1 implemented (2026-10-29)
- [x] Phase 1 accepted for LTI (tests + live job 216); DE paths are verified live in Phase 3's acceptance
- [ ] Phase 2 implemented
- [ ] Phase 2 accepted (tests + live 20-site timed run)
- [ ] Phase 3 implemented
- [ ] Phase 3 accepted (≤25 s online lane, ≤30 s offline lane, screenshots unchanged)
- [ ] Phase 4 decision: needed? (based on Phase 3's measured DE projection)
- [ ] Phase 4 spike + implementation + acceptance (if needed)
- [ ] Phase 5 accepted: full fleet under 8 h
