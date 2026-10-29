# Initial plan — Rejection-service CSV audit report (turn on + FX/DS screenshot per serial)

- `Discussed:` 2026-09-10 — user asked to feed `August Rejections Service(Sheet1).csv` into the DST project and produce a report with a screenshot per camera.
- `Stage:` plan
- `Related:` none (no other plan files exist yet)

## Problem

The rejection CSV has three columns:

```
Row Label,Count of Reason,Rejection Reason
DS012167,1,No FIM Video
FX1263,1,No FIM Video
...
```

- `Row Label` is the camera serial — two families: `DS######` (LTI pole cameras) and `FX####` (DragonEye).
- `Count of Reason` and `Rejection Reason` are context we should preserve verbatim in the output.

We want a repeatable job that:

1. Reads this CSV.
2. Matches each `Row Label` serial to a local DST `Installation` (the "serial in my DST project").
3. For each matched camera, attempts to power it on.
4. If power-on fails or errors, does nothing further to that camera and flags the row.
5. Otherwise captures a screenshot:
   - **FX** → TeamViewer screenshot (existing DragonEye path).
   - **DS** → VNC screenshot (existing LTI path).
6. Emits a report with the original three columns plus the result/screenshot.

## Proposed approach

Reuse the existing fleet-audit machinery instead of building parallel capture logic. The `CONFIRM_*` batch is the closest model: upload → match → parent job + child jobs → screenshots → detail page.

### 1. Input parsing (`services/`)

Add a small CSV parser for this exact three-column shape. The column name `Row Label` should be auto-detected (case-insensitive) but tolerate a bare first column too.

- New: `services/rejection_csv.py`
  - `parse_rejection_csv(text) -> list[RejectionRow]` where `RejectionRow` carries `serial`, `count`, `reason`, `source_row`.
  - Normalize serials case-insensitively and strip whitespace.
  - Keep `Count of Reason` / `Rejection Reason` as strings (do not coerce — some values may be non-numeric or blank).

### 2. Serial matching (`services/`)

Extend/reuse `services/ims_export_match.py`. Its `match_ims_export_to_installations()` already builds a serial index from `Installation.serial_number`, `camera_a`, `camera_b`, `camera_c`, and `fx_numbers`, and already resolves a `kind` of `lti` vs `de`. Add a parallel entry point for the rejection shape:

- New: `services/rejection_match.py`
  - `match_rejection_rows_to_installations(rows) -> list[RejectionMatch]` with the same statuses the confirm flow uses: `matched`, `unmatched`, `ineligible`, `duplicate`, plus `kind` (`lti` / `de`).
  - Match order: exact serial token → FX number. Preserve first-match-wins to avoid one installation being driven twice.
  - `FX####` matches `inst.fx_numbers`; `DS######` matches `camera_a` / `camera_b` / `camera_c` / `serial_number`.

**To verify during implementation:** confirm which field actually holds the `DS######` values (expected `camera_a`/`camera_b` per `_build_lti_layers`), and extend the matcher to also index `InstallationDevice.unit_serial` if any DS serial lives only there.

### 3. Power-on per serial

- **FX** — reuse `services/ovrc_capture.ensure_dcam_on_for_fxes([fx], ...)` (one FX at a time, or batched) instead of the whole-site `_power_on_de_site`. The result already distinguishes `already_on` / `turned_on` / error.
- **DS** — reuse `_power_on_lti_site()` / `services.cbw_relays.turn_all_relays_on()` for the installation's pole. Note: CBW relays are pole-scoped, so a DS serial powers on its pole; multiple DS rows that share a pole should have their relay turn-on deduplicated to avoid hammering the same pole.

If any power-on call raises or returns an error/`None` result, mark that row **"unable to be turned on"** and skip capture for it.

### 4. Capture per serial

- **FX** — reuse `services.teamviewer_capture.capture_dragoneye_via_teamviewer()`, but scoped to the single FX serial. Add a helper that resolves just that FX's TeamViewer lane(s) (reuse `services.device_layers.resolve_de_tv_capture_targets(inst)` filtered by `fx`, or `mappings_for_fx(fx)`), then capture and save via `_save_shot`.
- **DS** — reuse `services.vnc_capture.capture_vnc()`. Add a helper `resolve_ds_vnc_target(inst, ds_serial) -> (host, lane)`:
  - Compare `ds_serial` against `camera_a` (→ L1) and `camera_b` (→ L2), falling back to `InstallationDevice.unit_serial` groups.
  - Derive the VNC host the same way `_build_lti_layers` does: `tf_a_ip` / `tf_b_ip` if present, else `pole_to_ip_address(pole, DeviceType.TF_CPU, lane)`.
  - Capture just that lane's screenshot (single image per DS row).

### 5. Output report

**Confirmed decision (2026-09-10):** the deliverable is a **single Excel `.xlsx`** that keeps the original three columns and adds two columns:

- `Result` — `OK` / `UNABLE TO TURN ON` / `UNMATCHED` / `NO TEAMVIEWER ID` / etc., plus a short error note.
- `Screenshot` — the embedded PNG image (downscaled via Pillow, anchored to the row).

No companion `.csv` — the `.xlsx` is the only artifact to keep track of. Raw PNGs still live under `media/audits/<job_id>/` as the existing screenshot storage.

### 6. Orchestration / job model

Mirror the `CONFIRM_BATCH` + `CONFIRM_SITE` pattern:

- Add `AuditJob.DeviceType` values: `REJECTION_REPORT` (parent) and `REJECTION_SITE` (child).
- Persist the parsed CSV rows + match decisions in a selection file under `media/audits/` (like `write_dst_selection`/`write_confirm_selection`), so the background worker can re-read them.
- Add `audits/runner.py` `_run_rejection_report(job_id, output_dir)`:
  1. Load rows + matches.
  2. Phase 1 — power-on (FX batched through one OvrC login; DS relays in parallel, deduped by pole). Record per-row on/off/error.
  3. Phase 2 — capture only matched-and-powered-on rows (FX TeamViewer, DS VNC), creating a child `REJECTION_SITE` job per camera for the existing detail/screenshot UI.
  4. Phase 3 — build the `.xlsx` output and register it for download.
- Route the new device type in `_run_job`'s dispatch (add a branch beside `CONFIRM_BATCH`).

### 7. Web UI / routes

Add a `rejection-report` page parallel to `confirm_captures`:

- `GET /audits/rejection-report/` — upload form + recent jobs.
- `POST /audits/rejection-report/preview/` — parse CSV and return match JSON (matched/unmatched/duplicate counts + per-row kind).
- `POST /audits/rejection-report/start/` — persist rows, enqueue `REJECTION_REPORT` parent job.
- `GET /audits/rejection-report/<job_id>/download/` — serve the generated `.xlsx`.

Reuse `audit_job_status` SSE/progress and the existing `detail.html` screenshot rendering.

Optionally also add a management command (`manage.py rejection_report input.csv --output out.xlsx`) for headless/operator use — matches how `sync_installations` is exposed.

## Files to add / change

- Add `services/rejection_csv.py` — parse the three-column CSV.
- Add `services/rejection_match.py` — match serials to `Installation`, reuse `ims_export_match` internals.
- Add `services/rejection_report.py` — build the `.xlsx` with embedded images.
- Change `audits/models.py` — add `REJECTION_REPORT` / `REJECTION_SITE` `DeviceType` choices.
- Change `audits/runner.py` — add `_run_rejection_report`, per-serial FX/DS power-on + capture helpers, and a dispatch branch.
- Change `audits/urls.py` + `audits/views.py` — add the four routes above.
- Add `templates/audits/rejection_report.html`.
- Optionally add `cameras/management/commands/rejection_report.py`.
- Update `README.md` (developer) + operator workflow doc if one exists.

## Phases

1. **Parsing + matching** — `rejection_csv.py` + `rejection_match.py`; unit tests for FX/DS/unmatched/duplicate.
2. **Preview endpoint + page** — upload CSV, show match counts and per-row kind before starting.
3. **Power-on + capture runner** — `_run_rejection_report` with deduped DS power-on, per-FX OvrC, and per-serial FX/DS capture; reuse existing `_save_shot`.
4. **Report generation** — build the `.xlsx` (embedded screenshots); add download route.
5. **CLI command** — optional `rejection_report` management command.
6. **Docs + validation** — `python manage.py test`, `python manage.py check`; run against the provided CSV with a small serial subset first.

## Risks / edge cases

- **Image embedding** — resolved: a single `.xlsx` is the deliverable (CSV can't hold images).
- **DS serial → lane ambiguity** — if a DS serial isn't in `camera_a`/`camera_b` or device `unit_serial`, the row must degrade to a clear "unable to locate VNC lane" result rather than guessing.
- **FX with no TeamViewer mapping** — already handled by `_run_de_bundle`; surface as `NO TEAMVIEWER ID` in the report instead of failing the whole job.
- **One installation matched twice** — dedupe by installation so a camera isn't powered/captured twice.
- **Power-on is pole-scoped for DS** — dedupe CBW relay calls by pole; a shared pole can satisfy multiple DS rows.
- **Long runtimes** — TeamViewer/OvrC are slow and exclusive; reuse the existing single worker-slot semaphore and cancellation flags.

## Status

- In progress — implementation complete (parse, match, runner, views, CLI, report builder, tests); live capture not yet validated against a camera.
