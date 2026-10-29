# Plan of action — per-device time check on the review page (2026-10-29)

Discussed: 2026-10-29, the user (DST Camera Audit thread), after live publish (FAST_FLEET_DST_AUDIT Phase 4b).
Related:
- plans/in-progress-plans/FAST_FLEET_DST_AUDIT.md: captures now run concurrently and out of order, which is why each needs its own time.
- audits/review.py, templates/audits/review.html: the review page.
- audits/ovrc_time.py: today's single OvrC reading and drift.

## Problem
The user's request:
- **Timestamps:**
  - give every capture its own capture timestamp, shown in Eastern;
  - keep each timestamp on the same row as its capture.
- **Per-device time check:**
  - read each device's clock;
  - work out what that device should show, in the site's own timezone (from the agency/location);
  - show the drift per device, on the capture's row;
  - add a quick pass/fail column per device.

What exists today (evidence):
- **One reading per site.** Each site job holds a single OvrC reading (`ovrc_*`, `drift_seconds`). Drift is measured against **Eastern** (`audits/ovrc_time.py`). Captures show only `HH:MM` under each thumbnail (`review.html`).
- **Eastern is wrong for non-Eastern sites.** Run 268's CBW at 233121 (Shreveport, LA) reads 09:12:19 at 10:12 Eastern: correct Central time. Spike VNC readings for 233201, 233040, 220054 and 210073 are ≈ −3,605 s against Eastern.
- **Where each device's clock comes from:**
  - **CBW:** Date/Time page text "Current: Thu, 29 Oct 2026 09:12:19". It's DOM text in the Selenium session we already have.
  - **VNC (LaserTech):** a 640×480 screen with the live clock under the bottom date ("10/29/2026 / 10:12:01 AM"). Image only.
  - **DragonCam (TeamViewer):** status-bar "Time: 10:15:02", plus Ubuntu's top-bar "Oct 29 10:15". Image only.
  - **OvrC FX:** already parsed ("Sun, Aug 02, 2026 01:11 AM EDT"), minute precision.
- **OCR spike** (`tmp/spike/device_time_spike.py`, Windows' built-in OCR via `winrt-Windows.Media.Ocr`, 0.1–0.2 s/image, every local capture):
  - VNC: 35/40 read. Typical device−capture drift −3…−7 s; the screenshot row is saved a few seconds after the grab.
  - DragonCam: 153/222 read. Whole-screen OCR misses the small status bar; reading the bar's full-width band at 2× fixed most. The top-bar fallback isn't working yet.
- **Site timezone spike** (`timezonefinder` on Installation GPS, 892 active):
  - Most resolve right; Florida and Tennessee are genuinely split (5 FL and 14 TN sites are Central).
  - Bad coordinates: 4 Iowa → Eastern, 3 Louisiana → Eastern, 1 Georgia → Central.
  - 54 sites have no GPS: CA 30, TN 10, FL 9, VA 3, GA 1, blank 1.

## Issue digest
- **T1 — Site timezone.**
  - The GPS zone is accepted only when it's valid for the site's state. Each single-zone state maps to one zone (Iowa and Louisiana Central; California Pacific; the rest Eastern). FL and TN allow both.
  - Otherwise, use a county list for FL/TN (FL panhandle west of the Apalachicola; TN's Central counties), then the state default.
  - The zone is stored on the site job at capture (`site_tz`), so the review server needs no lookup library.
- **T2 — Capture time.** Record the actual grab time per screenshot (`captured_at`); `created_at` is a few seconds later. It's shown in Eastern, as asked.
- **T3 — Device time per screenshot.** Store the text read, the wall clock and the source (`page` / `ocr` / `ovrc`) on the screenshot. A clock that can't be read shows "Unread — check the image", never a fail.
- **T4 — Drift and pass/fail per device.**
  - Drift = device wall clock − `captured_at` in the site's zone.
  - **Pass when |drift| ≤ 120 s** (the existing `DRIFT_FLAG_SECONDS`; OvrC has minute precision; a DST error is ±3,600 s).
- **T5 — OCR accuracy.** VNC 88 % and DragonCam 69 % in the spike. Unread devices fall back to manual review, so this degrades gracefully, but the target is ≥ 95 % on the local corpus.
  - **2026-10-29 finding: the DragonCam status-bar "Time:" is not the live clock.** Compared with Ubuntu's top-bar clock on 132 local captures where both read, it lagged in 32 (by 1–82 min; run 276's 255069: bar 10:50:00, top bar 10:52, grabbed 10:52:05). Using it would fail good devices. The reader now uses only the top-bar clock (`source=ocr-min`, minute precision: a correct clock reads up to 59 s behind, inside the 120 s pass). DragonCam reads 184/223 (83 %) on the local corpus this way; VNC 37/42 (88 %).
- **T6 — Production.**
  - New columns must exist in the shared Supabase DB, and the review server (Linode) needs the new template. Both are production steps: **ask the user first.**
  - Publish must copy the new screenshot fields.

## Phases

### Phase 1 — Record times at capture (gated)
Work:
1. Model fields and a migration:
   - `AuditScreenshot`: `captured_at`, `device_time_raw`, `device_time` (naive wall clock), `device_time_source`;
   - `AuditJob`: `site_tz`.
2. `audits/site_timezone.py`: GPS → zone, validated against the state; FL/TN county lists; state default; a reason when it falls back.
3. CBW: read the "Current:" text when the screenshot is taken.
4. OvrC: attach each FX reading to its own `ovrc_fx*` screenshot. The job-level fields stay for compatibility.
5. `services/device_clock.py`: Windows OCR readers for VNC and DragonCam, from the spike. Run right after each image is saved; failures leave the fields empty.
6. Publish copies the new fields.

Acceptance:
- Unit tests:
  - zone resolution, including the bad-GPS cases above;
  - CBW text parse;
  - OCR readers on fixture images (VNC, DragonCam, both readable and unreadable);
  - drift sign and the DST hour;
  - publish copies the fields.
- A local run of the 6 sites from run 268: every CBW/OvrC device has a time; VNC/DE read or "Unread".

### Phase 2 — Review page: one row per device (gated)
Work:
- Each installation spans one table row per capture (rowspan for camera, status, assigned, complete and notes).
- Per-capture columns: capture thumbnail · captured (Eastern) · device time · expected (site zone) · drift · Pass/Fail/Unread.
- The summary counts devices failing.
- "Drift flagged" filter: an installation with any failing device.
- Live updates (`review.js`) keep working on the first row of each installation.

Acceptance:
- Tests for the row building (pass/fail/unread, non-Eastern site, OvrC-only site).
- In a browser on the local review page:
  - a Louisiana site passes at −1 h against Eastern;
  - a thumbnail and its time sit on the same line;
  - complete/notes/assign still save and live-update.

### Phase 3 — Shared DB migration and review deploy (gated; production; needs the user's go-ahead)
Work: apply the migration to the shared DB; deploy the review instance; publish a test round.

Acceptance: the review server shows per-device rows for that round.

### Phase 4 — OCR accuracy to ≥ 95 % (gated)
Work: improve the readers on the failing local captures (top-bar fallback, crop/scale choices). Keep the misses as fixtures.

Acceptance: ≥ 95 % read on the local corpus; no wrong reading among the readable ones (each checked against the image).

## Progress
- 2026-10-29 Phase 1 implemented: fields + migration `0013_device_times`, `audits/site_timezone.py`, `audits/device_drift.py`, `services/device_clock.py`, CBW "Current:" sidecar, OvrC per-shot readings, publish copies the fields. Tests: `audits/test_device_time.py` (zone, drift/DST hour, CBW parse, OCR fixtures in `audits/testdata/clock/` incl. an unreadable VNC and the lagging-status-bar DragonCam), `test_publish`. Live run 276 (4 sites, cancelled before DE finished) already stored CBW/VNC/DE readings and `site_tz`, with 233121 resolved to Central.
- 2026-10-29 Phase 2 implemented: `audits/review.py` (per-capture `Capture` checks; older runs fall back to the job's OvrC fields and the agency/state zone with no GPS), `templates/audits/review.html` (one `<tbody class="review-site">` per camera, a line per capture, shared cells rowspanned), `static/review.js` (state keyed on the tbody), `static/app.css`. Full suite 205 tests OK.
- 2026-10-29 Phase 2 browser check (local copy of the DB, runs 268 + 276 as a round, headless Chrome): 233121 (Shreveport, LA) CBW 09:50:47 CDT passes at +0s; each thumbnail sits on the same line as its captured / device / should-be / drift / check; an hour-off VNC shows Fail, "1 device failing", and the "Failing a time check only" filter shows just that camera; Complete, assign and notes saved, live-updated the header/chip and survived a reload; no console errors.

## Checklist
- [x] Phase 1 implemented
- [ ] Phase 1 accepted (tests + local 6-site run): tests pass; waiting on a full 6-site run (run 276 was cancelled mid-DE)
- [x] Phase 2 implemented
- [x] Phase 2 accepted (tests + browser check)
- [ ] User go-ahead for production (shared DB migration + review deploy)
- [ ] Phase 3 accepted
- [ ] Phase 4 accepted (≥ 95 % OCR on the local corpus)

## Status
in progress — Phases 1 and 2 implemented; Phase 1 waits on a full local 6-site run; Phase 3 needs the user's go-ahead.
