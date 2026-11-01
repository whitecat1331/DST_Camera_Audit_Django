# Initial plan — CBW firmware inventory report (CBW + firmware + full IMS location)

- `Discussed:` 2026-10-06 — coworker needs a list of every CBW with its firmware version, plus the full IMS installation record so they know where each CBW physically is. Output: **CSV**.
- `Stage:` plan
- `Related:` `AUGUST_REJECTIONS_CSV_REPORT.md` — same "report from IMS data + field capture" shape; reuse its management-command + export patterns and the CBW auth/capture helpers.

## Problem

There is currently no place in either codebase that records a CBW's **firmware version** (verified: `grep -i firmware` returns nothing in `IMS/*.go` or `DST_Camera_Audit/**/*.py`). The firmware can only be read live from each CBW device.

The "where is it" data does exist, split across two places:

- **IMS** owns the full installation record — `Installation` (`IMS/models_installations.go`) carries `PoleNumber`, `Identifier`, `Name`, `Street`, `County`, `Agency`, `Location`, `State`, `Direction`, `SiteType`, `PowerType`, `ProductKit`, `GPSLat/GPSLon`, `SerialNumber`, `FLNumber`, `IPAddress`, `Vendor`, `Model`, `Notes`, etc.
- **DST** already mirrors a *subset* of that into SQLite (`cameras.models.Installation`) via `services/ims_sync.py`, and already knows how to reach a CBW (`services/ip_map.py`: CBW = last octet `177` of the pole-derived `10.x.y.177` address).

So the join is easy (pole number / CBW host). The two gaps are: (a) the firmware itself, and (b) the full IMS fields aren't exposed through the DST-facing API yet.

## Proposed approach

One report command in DST (`manage.py cbw_firmware_report`), backed by two small enabling changes (IMS API + DST sync/model). CBWs are enumerated from IMS device records, firmware is read live from each CBW, and the full IMS record is joined in and written to CSV.

### 0. Discovery — where does a CBW expose firmware? ✅ DONE

Confirmed against `10.3.38.177` (pole `233038`, "Bainbridge HS") on 2026-10-06.
The CBW is a **ControlByWeb X-410-I**. Firmware is a plain HTTP GET — no Selenium needed:

- `GET /firmware.json` (basic auth) →
  `{"x spc1_partNum": ["X-410-I"], "x spc1_firmRev": ["3.13.7.9"]}`
- Firmware revision = `["x spc1_firmRev"][0]`; part number = `["x spc1_partNum"][0]`.
- Auth: the same `CBW_USERNAME` + `CBW_PASSWORDS` try-loop already used by
  `services/cbw_capture.py` `_probe_cbw_auth`.

Useful adjacent endpoints (same auth, optional extras):
- `GET /state.json` → `serialNumber` (`00:0C:C8:08:58:F3`), `deviceUptime`, `vin`,
  `lat`/`long`, flat `relay1..4` (confirms existing `cbw_relays.py` logic).
- `GET /ioNames.json` → `deviceName` (`233038 Bainbridge HS`) + serial — a
  cross-check that the CBW's configured name matches the pole.

Throwaway probe scripts (no secrets — creds read from `.env` at runtime): `tmp/cbw_probe*.py`.

**Follow-up during implementation:** the fleet spans several firmware generations,
but `GET /overview.json` exposes `x spc1_firmRev` / `x spc1_partNum` on **all** of
them (observed `2.2`, `2.5`, `3.06`, `3.13.7.9`). `/firmware.json` only exists on
**v3.12+** (per ControlByWeb's v3.12 release notes: "Devices on v3.12 will now have
a 'Firmware' tab"). So `overview.json` is the universal firmware source and the
report uses it (with `firmware.json` as fallback) — no legacy-UI Selenium needed.
Some CBW subnets are not reachable from the dev laptop (ConnectTimeout) — reported
`unreachable`.

### 1. IMS — expose the full installation record on `/api/external/ase-installations`

`IMS/handlers_external_api.go` `handleExternalASEInstallations` builds an `item` struct with ~20 fields (id, identifier, primary_platform, all_platforms, pole_number, serial_number, fl_number, ip_address, state, agency, location, status, gps_lat, gps_lon, vendor, model, updated_at + devices/components/inventory). The richer fields (`Street`, `County`, `Direction`, `SiteType`, `PowerType`, `ProductKit`, `Name`, `Notes`, `ParentInstallationID`, `Source`) are already loaded on the `Installation` struct by `listInstallationsPaginated` — they're just not serialized.

Done. Additive, backward-compatible change: added `name`, `direction`, `street`, `county`, `site_type`, `power_type`, `product_kit`, `subtype`, `parent_installation_id`, `source`, `notes`, `prtg_device_name`, `created_at` to the `item` struct and populated them from the already-loaded `Installation`. `listInstallationsPaginated` already selected most of these; `county`/`site_type`/`power_type`/`product_kit`/`subtype` were added to its SELECT + Scan in `assets_read.go`.

### 2. DST — persist the full fields

- Done — added `name`, `street`, `county`, `direction`, `site_type`, `power_type`, `product_kit`, `subtype`, `source`, `notes`, `prtg_device_name`, `parent_installation_id`, `created_at` (migration `0006_*`).
- Extend `services/ims_sync.py` `sync_installations_from_ims` to map them (mirror of the existing `defaults` dict).
- The CBW device rows already sync (`InstallationDevice.device_type == 'cbw'`, `host`). Note: the device `host` is the PRTG host (often the modem's **public** IP), NOT the probe address — the report probes `cbw_ip(pole_number)` (`10.x.y.177`), matching `services/cbw_relays.py`.

### 3. DST — `cbw_firmware_report` management command

Add `cameras/management/commands/cbw_firmware_report.py`:

1. **Enumerate CBWs** — `Installation.objects.filter(devices__device_type='cbw', is_active=True).distinct()`. Fallback: any active LTI/ALPR/ASE installation with no CBW device row gets its derived CBW (`cbw_ip(pole_number)`) included and flagged "derived, not in IMS device list".
2. **Read firmware** — for each CBW, `GET /firmware.json` via `services/cbw_firmware.py` (same `CBW_PASSWORDS` try-loop), probing the pole-derived `10.x.y.177` address. Emit `cbw_status` + `cbw_error` (`ok` / `no_firmware` for older firmware / `unreachable` / `auth_failed` / `no_cbw`); never drop a row. `--concurrency N` probes in parallel (ThreadPoolExecutor).
3. **Join** — pole number (or CBW `host`) → the (now-full) `Installation` row.
4. **Write CSV** — `csv` stdlib, one row per CBW.

### 4. CSV schema

One row per CBW, columns:

| Group | Columns |
|---|---|
| CBW | `cbw_ip`, `cbw_firmware`, `cbw_part_number`, `cbw_serial`, `cbw_source`, `cbw_status`, `cbw_error` |
| Identity | `ims_id`, `identifier`, `name`, `primary_platform`, `all_platforms`, `pole_number`, `serial_number`, `fl_number`, `ip_address`, `vendor`, `model`, `status` |
| Location | `agency`, `location`, `street`, `county`, `state`, `direction`, `site_type`, `gps_lat`, `gps_lon` |
| Other | `power_type`, `product_kit`, `subtype`, `source`, `notes`, `prtg_device_name`, `created_at`, `ims_updated_at` |

## Files to add / change

**IMS**
- Change `handlers_external_api.go` — add full installation fields to the `ase-installations` `item` struct.
- Change `assets_read.go` — add `county`/`site_type`/`power_type`/`product_kit`/`subtype` to the `listInstallationsPaginated` SELECT + Scan.

**DST Camera Audit**
- Change `cameras/models.py` — new `Installation` fields.
- Add `cameras/migrations/0006_*.py` — schema change.
- Change `services/ims_sync.py` — map new fields.
- Add `services/cbw_firmware.py` — firmware read + `cbw_status` classification.
- Add `cameras/management/commands/cbw_firmware_report.py` — the report.
- Add `cameras/test_cbw_firmware.py` — reader unit tests (mocked HTTP).
- Change `README.md` — document the new command.
- Change `.env` — `IMS_BASE_URL` → `http://127.0.0.1:8051` (standalone `sync_installations` reads `.env`, not the `--ims` flag).

## Phases

1. **Discovery** — ✅ done: firmware is `GET /firmware.json` → `x spc1_firmRev` / `x spc1_partNum` (see §0).
2. **IMS API** — ✅ done: `go build` clean; verified end-to-end via DST sync (849 installs fetched with the new fields).
3. **DST sync/model** — ✅ done: migration `0006` applied; `manage.py check` clean; sync populated `name`/`street`/`county`/`direction`/`site_type`/`power_type`/`product_kit`/`source`/`created_at`.
4. **Firmware reader** — ✅ done: `services/cbw_firmware.py` + 5 mocked-HTTP unit tests passing.
5. **Report command** — ✅ done: smoke-tested on pole `233038` (firmware `3.13.7.9`, full IMS row joined).
6. **Docs + validation** — ✅ mostly done: README updated; `manage.py test cameras audits` (25) + reader tests (5) green. **Remaining: full-fleet run** (`manage.py cbw_firmware_report` with no `--limit`) and eyeball the CSV.

## Risks / edge cases

- **Firmware source** — resolved: `GET /overview.json` returns `x spc1_firmRev` / `x spc1_partNum` on every firmware generation (2.x → 3.13.x). `firmware.json` (v3.12+) is a fallback.
- **Probe address** — use the derived `10.x.y.177`, not the PRTG `host` (which is often the modem's public IP and not HTTP-reachable from the dev laptop).
- **CBWs not in IMS device list** — some poles may have a CBW in the field but no `device_type='cbw'` row; the derived-IP fallback catches these but may over-report (probe reachability to confirm).
- **Unreachable / bad creds** — must not fail the whole run; emit `cbw_status` and keep the row so the coworker sees the gap.
- **Credential handling** — reuse `CBW_USERNAME`/`CBW_PASSWORDS` from settings; never log Basic-auth URLs (existing code already strips creds from logs — follow the same pattern).
- **Runtime** — a fleet run is one HTTP hit per CBW (fast) unless the Selenium path is required; keep a `--limit N` / `--poles A,B,C` filter for partial runs.
- **API field list drift** — keep the IMS change additive so existing DST/PatsScraper consumers are unaffected.

## Status

- In progress — implementation complete and full-fleet run done (2026-10-06): 519 CBWs probed → 347 `ok`, 137 `unreachable`, 35 `auth_failed`. Not yet marked complete — awaiting your review/approval.
