# Plan of action — DST Audit shared review (capture locally → publish to Supabase → one-page review with Complete + Notes) — 2026-10-27

- `Discussed:` 2026-10-27 — chat with Patrick. The DST audit has to run on the operator's laptop because only it can reach the camera network (CBW `10.x.y.177`, VNC), but other people need to review the results as one list, mark each camera audited, and share notes.
  - Options weighed: (A) an export bundle viewed in a separate app; (B) export/import pages inside DST; (C) local DST and the Linode DST sharing one Supabase database; (C′) **local capture into SQLite, then publish finished runs to Supabase; the Linode DST reviews from Supabase.**
  - C was chosen first, then replaced by **C′** after Phase 0 proved the camera VPN blocks Postgres (I2) and IT won't open ports.
- `Stage:` plan → promoted to `plans/in-progress-plans/` 2026-10-27, when the user directed the `.env` / settings work to start.
- `Thread:` Claude Code session 2026-10-27 (DST audit shared review); not a Zed thread.
- `Workspace:` `C:\Users\ployd\Desktop\DST_Camera_Audit`
- `Related:` `in-progress-plans/CBW_FIRMWARE_INVENTORY_REPORT.md`, `in-progress-plans/AUGUST_REJECTIONS_CSV_REPORT.md` — no overlap or conflict. Both are report commands that keep running on the laptop's SQLite, which this plan does not move.

## Problem

- **Results are stuck on the laptop.** A DST fleet run is one `DST_AUDIT` `AuditJob` with one `DST_SITE` child per site (`audits/runner.py:2341`). Each child has `AuditScreenshot` rows whose images live on local disk under `media/audits/<job_id>/` (`audits/models.py:66`). The database is local SQLite. Nobody else can see a run.
- **Reviewing means clicking through job by job.** There is no single list of installation → devices → captures → timestamps → device time.
- **OvrC device time is not a real field.** It is packed into `AuditJob.progress_message` as `"OvrC FX1261: …"` and parsed back out of the string (`audits/runner.py:692`, `audits/runner.py:699`).
- **Nowhere to record review state.** There is no shared "this camera has been audited" mark and no shared notes.
- **Network constraint (proven in Phase 0):** the camera VPN blocks outbound Postgres, so the laptop can reach Supabase **only with the VPN off**.
- **Scale today:** `media/audits` holds 556 images, 603 MB, median 1.4 MB each (full-size PNGs), measured 2026-10-27.

## Decisions made

| Decision | Choice |
|---|---|
| Data flow | **The laptop always captures into local SQLite** (VPN on or off). After the runs, with the VPN off, **"Publish to shared DB"** copies finished runs to Supabase. **Only the Linode DST reads and writes Supabase** (review page, Complete, Notes). |
| Audit day | 1) VPN **on**: DST Audit with **LTI only** (CBW + VNC). 2) VPN **off**: DST Audit with **DE only** (TeamViewer + OvrC over the internet; `services/teamviewer_capture.py` and `services/ovrc_capture.py` have no `10.x`/camera-network dependency). 3) VPN off: publish both runs into one **audit round**. |
| Audit round | Publishing tags runs with a round (e.g. "2026-11-01 fall back"). The review page shows **one row per camera per round**, using the newest site capture in the round, so the LTI run, the DE run and any re-runs merge into one list. |
| Review state | A shared **Complete** checkbox plus a shared **Notes** field per **camera per round**. Records who changed it and when. The next round starts unchecked. |
| Roles | `DST_ROLE=capture` (laptop, default): `default` = SQLite, plus a `shared` alias from `DB_*` used only by publish. `DST_ROLE=review` (Linode): `default` = Supabase from `DB_*`. |
| Image storage | The laptop keeps files on disk (unchanged). Publish writes image bytes **plus a thumbnail made on the laptop** into a Supabase `bytea` table. The review instance serves them through a database-backed Django storage backend. |
| Connection | Supabase **transaction pooler** (6543, `postgres.<project-ref>`, `verify-full` with `certs/supabase_ca.crt`), same as IMS (`IMS/db.go:48`). `migrate --database shared` and bulk work over the **session pooler (5432)**. `connect_timeout` 10 s per address. |
| Supabase project | A new project in **`aws-1-ca-central-1`**, separate from IMS (project ref checked against IMS dev and prod). Compute: **Micro (1 GB)**. |
| Hosting | The **IMS Linode on port 8443** (cost). Details under Hosting. |

**Sizing note.** For the database, Micro (1 GB) is enough. The data is about 0.6 GB, there are a handful of users, and the review page loads thumbnails (~30–60 KB). Moving to Small (2 GB) is worthwhile only if the review page acceptance shows slow page loads.

## Environment variables

| Variable | Laptop (`capture`) | Linode (`review`) | Docker secret on Linode |
|---|---|---|---|
| `DST_ROLE` | `capture` (default) | `review` | env in compose |
| `DB_HOST` | Supabase pooler host. Enables the `shared` alias; does **not** change the working DB. | Required | env |
| `DB_PORT` | `6543` (5432 for `migrate --database shared`) | `6543` | env |
| `DB_USER` | `postgres.<project-ref>` | same | `/run/secrets/db_user` |
| `DB_PASSWORD` | set | set | `/run/secrets/db_password` |
| `DB_NAME` | `postgres` | same | env |
| `DB_SSLMODE` | `verify-full` | same | env |
| `DB_SSLROOTCERT` | `certs/supabase_ca.crt` (repo-relative) | same; certificate in the image or mounted | env |
| `DB_CONNECT_TIMEOUT` | `10` (fails fast on the VPN) | `10` | env |
| `DJANGO_SECRET_KEY`, `IMS_API_TOKEN`, `IMS_SSO_CLIENT_SECRET` | `.env` | — | `/run/secrets/django_secret_key`, `ims_api_token`, `ims_sso_client_secret` |

- Secret files are loaded by `_load_secret_file` in `config/settings.py`, which mirrors IMS `config.LoadSecretFile`. A value already in the environment or `.env` wins.
- IMS's Go pool settings (`DB_MAX_OPEN_CONNS`, …) are deliberately **not** wired. Django has no client-side pool, and DST keeps `CONN_MAX_AGE=0` under the pooler.

## Phases

Each phase is **gated**: its acceptance must pass before the next phase starts.

### Phase 0 — Spike: network reach (gated) — ACCEPTED 2026-10-27
- **Question, reframed after I2:** where can the laptop reach Supabase, and do DE captures need the VPN?
- **Evidence:** `tmp/spike_supabase_connect.py` (throwaway).
  - **VPN off: PASS.** 6543 connected in 0.44 s and 5432 in 0.35 s; `SELECT 1` OK; PostgreSQL 17.11; `search_path` = `"$user", public, extensions` (not empty); 2 MB `bytea` round trip 1.28 s and 1.23 s with the data intact.
  - **Camera VPN (OpenVPN TAP, full tunnel, `10.100.80.5`): 5432 and 6543 blocked.** 443 is open to the internet, the Supabase pooler and the IMS Linode. Linode port 22 is blocked. CBW `10.3.38.177:80` is open (ICMP is blocked).
  - **DE capture code** has no camera-network dependency (grep of the TeamViewer, OvrC and DragonEye services).
- **Result:** publish runs with the VPN off; LTI capture runs with the VPN on; DE capture works either way. Proving DE capture off-VPN at runtime is part of the final end-to-end acceptance.

### Phase 1 — Roles, shared schema, Supabase locked down (gated) — ACCEPTED 2026-10-27
- **Work:**
  1. ✅ `config/settings.py`: `DST_ROLE` capture/review, `DB_*` → `shared` alias (capture) or `default` (review), Docker-secret loader, `connect_timeout`. `requirements.txt`: `psycopg[binary]`. `.env.example` documented. `certs/supabase_ca.crt`.
  2. ✅ Supabase locked down **in the database** (it doesn't depend on the dashboard Data API toggle, which can't be checked without the anon key: `/rest/v1/` answers 401 "No API key" either way):
     - all table, sequence and function privileges revoked from `anon` and `authenticated` in `public`;
     - default privileges for role `postgres` revoked too, so future tables start out locked;
     - RLS enabled on every table.

     Originally `tmp/lockdown_shared.py`; promoted in Phase 2 to `manage.py lock_shared_db` (the tmp script was deleted). Run it after every `migrate --database shared`, because RLS is not turned on automatically for new tables (the privilege revocation is).
  3. ✅ `manage.py migrate --database shared` over 5432 with the VPN off: all 30 migrations applied (admin, audits 0001–0006, auth, cameras 0001–0006, contenttypes, sessions).
  4. ✅ `audits/apps.py`: the orphan-job reaper returns early when `DST_ROLE == "review"`. It already ran only under `runserver`, and the Linode runs gunicorn; this guard is defense in depth.
- **Accepted when:**
  - `manage.py check` is clean in both roles.
  - Capture with `DB_HOST` set still uses SQLite for `default`. ✅ Verified 2026-10-27.
  - `migrate --database shared` is clean. ✅
  - The public roles can't read the tables. ✅ Before the lockdown, `anon`/`authenticated` could SELECT **19/19** tables (including `auth_user` and `django_session`) and RLS was off on 19/19; after it, 0/19, 0/19 and RLS on everywhere. Running as `anon` itself: `permission denied for table auth_user`. A table created afterwards (probe, rolled back) is not readable by `anon`/`authenticated`. Django (owner role) still reads `shared` normally; local `default` still has 171 `AuditJob` rows on SQLite.
  - Reaper: with `DST_ROLE=review` + `runserver` it is not scheduled; with `capture` it is. ✅
  - On the VPN, any `shared` access fails within ~20 s instead of hanging. ✅ Verified: about 23 s with two addresses at 10 s each.

### Phase 2 — Stable IDs, rounds, structured OvrC time (gated) — ACCEPTED 2026-10-27
- **Work (done):**
  1. `audits/models.py`: `public_id` UUID (unique) on `AuditJob` and `AuditScreenshot`; an `AuditRound` model plus a nullable `AuditJob.round` FK; OvrC fields on `AuditJob`: `ovrc_fx`, `ovrc_local_time_raw`, `ovrc_zone`, `ovrc_device_time` (absolute instant from the zone abbreviation), `ovrc_captured_at`, and `drift_seconds`.
  2. **Drift definition (changed from the draft):** `drift_seconds` is the device's **wall clock** minus **America/New_York wall clock at the scrape moment**, not `finished_at`. Evidence: FX1261 recorded `02:10 AM ADT` at 05:11 UTC (01:11 Eastern), which is right in absolute time but an hour ahead on the wall clock. That wall-clock error is the DST signal, and comparing absolute instants would hide it. OvrC shows minutes only, so drift is accurate to about ±60 s.
  3. `audits/ovrc_time.py` (new, pure functions): `parse_ovrc_local`, `ovrc_fields`, `ovrc_fields_from_line`, and `OVRC_FIELD_NAMES`. It handles the current scrape format, the legacy converted `Aug 1, 2026, 11:47 p.m. EST` format, a missing zone, and garbage input.
  4. `services/ovrc_capture.py`: `OvrcTimeResult.captured_at` (UTC, set right after the scrape). `audits/runner.py`: `_capture_ovrc_times` and `_dst_fleet_ovrc_times` write the fields next to the existing progress line, which views still read, so no view changes were needed.
  5. Migration `audits/0007_publish_ids_rounds_ovrc_fields` (hand-written: nullable UUID → backfill → unique; the OvrC backfill uses `finished_at` because legacy rows have no scrape time). Migration `audits/0008_sync_device_type_status_choices`: **pre-existing drift** from `82bfb63` (rejection job types added without a migration); a no-op in SQL.
  6. `audits/management/commands/lock_shared_db.py` (replaces `tmp/lockdown_shared.py`): `--check` exits non-zero if exposed, and the default run applies the lockdown. **Run it after every `migrate --database shared`.**
- **Acceptance evidence:**
  - `manage.py test audits`: **23 passed** (15 new in `audits/test_ovrc_time.py` plus the existing rejection tests). `makemigrations --check`: no changes.
  - Local SQLite (backed up first to `tmp/db.sqlite3.pre-0007.bak`): 0007/0008 applied. 171/171 jobs and 225/225 screenshots have distinct `public_id`s. **7/7** jobs with an `OvrC FX…` line were backfilled with drift: FX1260 EDT −0.6 / −0.1 min, **FX1261 ADT +59.4 / +58.9 min** (the known wrong-zone device).
  - Shared DB (VPN off, 5432): 0007/0008 applied. `lock_shared_db --check` **failed as designed**: the new `audits_auditround` table had the API roles locked out (default privileges work) but **RLS off**. `lock_shared_db` → 20/20 tables locked; re-check passes.
  - **Live OvrC capture** (job 172, `I-VBE-0011` / FX1219, read-only scrape): status succeeded; fields written: `EDT`, raw `Tue, Oct 27, 2026 05:19 PM EDT`, `ovrc_captured_at` 21:19:48 UTC, **`drift_seconds = -48`** (minute truncation; the device is correct).
- **Carry into Phase 5:** set the review page's drift flag threshold above the ±60 s resolution (suggest |drift| ≥ 120 s), and show `ovrc_zone` next to the time so a wrong-zone label like ADT stands out.

### Phase 3 — Publish to shared DB (gated) — ACCEPTED 2026-10-27
- **Work (done):**
  1. `audits/publish.py`: `publish_runs()` (core), `start_publish_job()` (background thread tracked as a new `AuditJob.DeviceType.PUBLISH` job; one publish at a time), `check_shared_reachable()` (fails with "turn the camera VPN off" guidance), `publishable_runs()` (only `DST_AUDIT` parents whose parent and children are all finished), and `clean_round_name()`.
  2. **Order per run:** images first, each its own commit, skipped when the stored `sha256` matches; then **every row of the run in one transaction**. Rows upsert by `public_id`, `created_at` is restored after the upsert (`auto_now_add` would overwrite it), users are matched by username (otherwise null), and the run is tagged with the round in both databases.
  3. `StoredFile` model (`name` unique = the local storage name, `content_type`, `size`, `sha256`, `data`, a `thumb` JPEG of at most 480 px, timestamps). Migration `audits/0009_stored_file_publish_type`.
  4. `manage.py publish_dst_round --round NAME (--runs ID… | --latest N)`.
  5. A DST Audit page panel **"Publish to shared DB"** (`templates/audits/dst_audit.html`), shown only when `technician+`, `DST_ROLE=capture` and the `shared` alias is set. It lists finished runs with checkboxes and each run's round badge, has a round input with a datalist of existing rounds, a Publish button, and live status by polling `/audits/<job>/status/`. Endpoint `POST /audits/dst/publish/` (`views.publish_dst`).
  6. Tests: under `manage.py test`, `shared` is a second SQLite DB (`config/settings.py`), so CI never touches Supabase.
- **Acceptance evidence (VPN off):**
  - `manage.py test audits`: **32 passed** (9 new in `audits/test_publish.py`: copy, idempotent republish, changed image re-uploaded, unfinished/non-DST runs refused, empty round refused, missing file reported, unreachable DB message, user mapping).
  - Migration 0009 applied locally and on `shared`. `lock_shared_db` re-locked the new `audits_storedfile` table (RLS off → on; 21/21).
  - **A real audit day, mirrored:** LTI run **#159** + DE run **#77** published into one round "Acceptance test 2026-10-27" over 6543: 5 jobs, 8 screenshots, 8 images in **20.9 s**. `tmp/verify_publish.py`: every field, parent link and round matches, and **8/8 images byte-identical** (sha256 local = stored = recomputed). Thumbnails are 10–33 KB against originals up to 2.5 MB.
  - **Republish:** 0 uploaded, 8 unchanged, no duplicate rows.
  - **Killed mid-publish** (run #74, SIGKILL after 9 s): 3/6 images uploaded and **no #74 rows in shared**, so reviewers never see a half run. Re-run: 3 uploaded, 3 unchanged; verification PASS.
  - **UI/endpoint** (Django test client against the real DBs, an existing technician user): the panel renders and lists run #70 with the round in the datalist; an empty round gives 400 "Round name is required."; publishing #70 → job 173 **succeeded** (3 jobs, 6 screenshots, 6 images); after a reload, run #70 shows the round badge. Verification PASS.
  - **Blocked port (simulates the camera VPN; `DB_PORT=6544`):** fails in **23 s** with "Can't reach the shared DB … If the camera VPN is on, turn it off and publish again." This matches the 23 s measured on the real VPN in Phase 1.
- **Test data left in place:** the shared DB now holds round **"Acceptance test 2026-10-27"** with runs #159 (LTI), #77, #74 and #70 (DE): 11 jobs, 20 screenshots, 20 images. Locally those runs carry the same round tag, plus publish job #173 and OvrC job #172. These are kept as fixtures for Phases 4–5. **Delete that round from both DBs before the first real round** (checklist item).

### Phase 4 — Review instance on the IMS Linode :8443 (gated)
- **Work:**
  1. `DatabaseStorage` plus a signed-in `media/<name>` view that serves `StoredFile` bytes and thumbnails with `ETag`/`Cache-Control`.
  2. The review role **hides and rejects (403)** every capture, relay, OvrC, cancel, confirm, VBE, rejection-run and publish endpoint, and the capture explorer (it reads local disk).
  3. Production settings: `CSRF_TRUSTED_ORIGINS`, `SECURE_*` cookies, `ALLOWED_HOSTS`, whitenoise, and `IMS_CA_FILE` to trust IMS's self-signed certificate (instead of `IMS_TLS_VERIFY=false`).
  4. Dockerfile plus a compose project at `/opt/dst_camera_audit`, as described under Hosting.
  5. `sync_installations` on the Linode against IMS.
  6. User steps: IMS SSO redirect URI `https://45.79.220.29:8443/accounts/ims/callback/`; open 8443 in ufw / Linode Cloud Firewall.
- **Accepted when:**
  - `https://45.79.220.29:8443/` signs in through IMS SSO and shows published jobs with their images.
  - Capture POSTs return 403.
  - IMS on 443 is unaffected (healthcheck green, memory the same before and after), and DST stays under its 512 MB limit.
  - **A reviewer opens :8443 from their normal network** (I3).

#### Phase 4 progress (2026-10-27) — local half implemented and verified; deploy pending (not accepted)
- **Built:**
  - `audits/storage.py` `DatabaseStorage`: read-only over `StoredFile`. `url()` → `/media-db/<name>`, so every existing `shot.image.url` works unchanged.
  - `audits/media_views.py` `stored_media`: signed-in only, `?thumb=1`, ETag = sha256, `Cache-Control: private, max-age=86400`; falls back to the original when there's no thumbnail.
  - Review-only guard:
    1. `cameras/roles.py`: in `DST_ROLE=review`, `role_at_least()` is False for anything above `readonly`. This hides **and** rejects all 33 technician-gated capture controls in one place.
    2. `config/middleware.py` `ReviewOnlyMiddleware`: 403 for `/explorer/`, `/sync/`, `/dragoneye/`, `/admin/` and every non-GET request outside `/accounts/`. **Phase 6 must add its Complete/Notes endpoint prefix to `REVIEW_WRITE_PREFIXES`.**
  - Settings: `STORAGES` per role; WhiteNoise (manifest storage in review); `DJANGO_CSRF_TRUSTED_ORIGINS`; secure cookies, nosniff and referrer policy when `DEBUG` is off (no HSTS: bare IP + self-signed). `IMS_CA_FILE` → `services/ims_client._tls_verify()` **pins IMS's self-signed certificate**. The chain is verified, but the hostname check is off because the cert is for a bare IP with no SAN, which Python can never match.
  - `requirements-review.txt` (slim), `Dockerfile` (python:3.12-slim, collectstatic at build, non-root uid 10001, gunicorn TLS on 8443 with 2 workers × 4 threads), `.dockerignore`, `deploy/docker-compose.yml` (512 MB limit, file secrets, IMS cert/key mounted read-only, healthcheck), and the `deploy/README.md` runbook. `.gitignore`: `certs/*` + `!certs/supabase_ca.crt` (the public CA must ship in the `git archive`).
- **Verified locally:**
  - `manage.py test`: **62 passed** (whole project; 9 new in `audits/test_review_mode.py`). `check` is clean in both roles.
  - **Review mode against the real Supabase data, with selenium/pyvnc/pyautogui/pygetwindow/webdriver-manager import-blocked** (`tmp/review_mode_check.py`, `DJANGO_DEBUG=false`):
    - published job #5 detail renders with its image from the DB (sha256 match); thumbnail 32 KB JPEG; ETag → 304; unknown → 404; signed out → login redirect;
    - start button and publish panel hidden; 8 capture/admin/explorer/sync paths → **403 even for an `admin`-role user**.
  - Dependency probe: the review instance needs `folium`, `numpy`, `requests` and `httpx`; `pandas`, `openpyxl` and `cryptography` are not needed.
  - **IMS pinning live:** IMS's public cert fetched over TLS matches the server file (SHA-256 `98:DB:…:05:E9`). Pinned → `GET https://45.79.220.29/healthz` **200 `{"status":"ok"}`**; wrong CA → refused (`CERTIFICATE_VERIFY_FAILED`).
- **Not done yet (production — ask before each step):**
  1. Commit the work (the deploy ships a `git archive` of a commit).
  2. Operator steps: IMS SSO redirect URI, open 8443, key-file readability (`deploy/README.md`).
  3. On the Linode: create `/opt/dst_camera_audit` + secrets, build, `up -d`, `sync_installations`.
  4. Then run this phase's acceptance list (SSO sign-in on :8443, IMS unaffected, < 512 MB, capture POSTs 403, and a reviewer reaching :8443 from their own network for I3).
- The image can't be built on the laptop (no Docker), so the first real build happens on the Linode in step 3.

#### Phase 4 deploy (2026-10-27) — provisioned and running; external access + SSO pending (not accepted)
Directed by the user ("provision the linode instance"), using the `access-replacement-prod-ssh` skill (`deploy@45.79.220.29`, SSH check OK).
- **Location changed to `/home/deploy/dst_camera_audit`:** `/opt` is root-owned and `deploy` has no passwordless sudo. Created with `umask 077`: `secrets/` (`db_user` and `db_password` from the laptop `.env`, CRs stripped to 29 and 15 bytes; a fresh 64-char `django_secret_key`; `ims_api_token` and `ims_sso_client_secret` = **placeholder `pending-ims-prod-credentials`**) and `.env` (`IMS_SSO_CLIENT_ID` placeholder).
- **Production IMS credentials are not on the laptop:** its `.env` targets dev IMS (`127.0.0.1:8051`). `IMS/cmd/mint-dst-integration` was **not** run against production, because its `ON CONFLICT` upsert overwrites an existing `dst_camera_audit` SSO client's secret and redirect URIs, and the production DB password is deliberately not on the laptop. They must come from IMS Admin → External Integrations.
- **Code:** `git archive` of `2defb37`; the compose file at `815bbd3`. Image `dst_camera_audit:review` 419 MB, built on the Linode in ~32 s. IMS stayed `healthy` and `/healthz` 200 throughout (load 0.57).
- **Two fixes** found by the first real build/run: `DB_SSLROOTCERT` defaults to the baked CA (`2defb37`: collectstatic imports settings, which correctly refuses `verify-full` without a CA); the healthcheck sends `Host: 45.79.220.29` (`815bbd3`: 127.0.0.1 is deliberately not an allowed host).
- **Running:** `dst_camera_audit-dst-1` healthy, **157 MiB / 512 MiB**. IMS `healthy` at 52–61 MiB (unchanged); host 1.3 GB available.
- **Verified on the box** (`https://127.0.0.1:8443` with the public Host header):
  - login 200; signed-out `/` and `/media-db/…` → 302 to login;
  - POST start/publish/turn-relays → **403**; `/explorer/` → **403**;
  - WhiteNoise `/static/app.css` 200; `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`;
  - :8443 presents IMS's cert (SHA-256 `98:DB:…:05:E9`).
- **Container → Supabase:** pooler 6543 `verify-full`, user and secret key loaded from the secret files, 11 published jobs and 20 stored files visible, `DatabaseStorage` active, `lock_shared_db --check` 0/0/0 of 21.
- **External access FAILS (I6):** from the laptop (VPN off), `https://45.79.220.29:8443` times out after 15 s, while the host listens on `0.0.0.0:8443`. A firewall is dropping it: `ufw` (status needs root) or a Linode Cloud Firewall.
- **Still pending for acceptance:** I6 firewall; production IMS SSO client + API token; I4 before `sync_installations`; then SSO sign-in on :8443, and a reviewer reaching :8443 from their own network (I3).

#### Phase 4 — production IMS credentials + first sync (2026-10-28)
- **Credentials:** the user's IMS account has TOTP 2FA, so a scripted login can't finish (one attempt failed on a trailing space in the user env var `IMS_PASSWORD`; the corrected attempt reached `/login/mfa`, which cleared the rate-limit counter). Instead, `tmp/provision_ims_via_browser.py` opened a **visible Chrome** (Selenium) at IMS `/login` after checking the server's cert against the pin `98:DB:…:05:E9`. **The user signed in (incl. 2FA)**; the script reused that session over the pinned client to `POST /admin/external-integrations/tokens/create` and `/sso/create`. Result: **API token "DST Camera Audit review (Linode :8443)"** (`ase_installations:read`) and **SSO client `dst_camera_audit_review`** (new id; IMS's create refuses duplicates, so no existing client was touched), redirect `https://45.79.220.29:8443/accounts/ims/callback/`. Both were piped over SSH straight into `~/dst_camera_audit/secrets/` (69 bytes each) and `.env`; nothing printed or stored on the laptop. Logged out, Chrome closed.
- **Container recreated:** settings report client id `dst_camera_audit_review`, secret and token loaded, redirect as above.
- **Bug found by the first Postgres sync, fixed (`d185cc4`):** `InstallationDevice.host` was `varchar(64)`, but 10 IMS devices have ControlByWeb cloud URLs up to **146** chars (e.g. `I-ASE-700002`). SQLite never enforced `max_length`, so only Postgres caught it (`DataError: value too long for type character varying(64)`). Widened to 255 (`cameras/0007_widen_device_host`), migrated locally + `shared`, `lock_shared_db` 21/21. Note: those URLs appear to embed a ControlByWeb cloud key; they were already stored in IMS and local DST, and in Supabase they sit behind the lockdown.
- **First production sync PASS:** `sync_installations` in the container → IMS API via the pinned cert (container → host's public IP): **847 rows fetched, 806 created + 41 updated, 3,441 devices**, longest host 146. 846/847 have `street`, 697 `site_type` (`county` is empty in IMS's payload today). Spot checks: `233038` → I-LTI-233038 Bainbridge GA; `I-VBE-0011` → New Kent Co VA; `266134` → I-DE-266134 Knoxville TN. Peak container memory 302 MiB during the sync (< 512); IMS healthy throughout.
- **Operational note:** a full sync takes ~6 min over the pooler (row-by-row device/sensor writes). Run it detached (`docker compose exec -d dst sh -c "python manage.py sync_installations > /tmp/sync.log 2>&1"`): an SSH drop mid-sync (the laptop's wired link went down) reset the session, though that run did complete.
- **Remaining Phase 4 acceptance:** (1) **the user signs in at https://45.79.220.29:8443 through "Sign in with IMS"** (2FA), and lands on the dashboard showing the published acceptance round; (2) I3, a reviewer opens :8443 from their own network.

#### Phase 4 — "loading extremely slowly" (user report 2026-10-28): measured and fixed
- **User report:** SSO sign-in on :8443 **works** (acceptance step 1 ✅ for the user `ployd`), but pages load extremely slowly.
- **Measured** (Linode → Supabase ca-central-1): each query is a **32 ms** round trip; a new pooler connection **~330 ms** (once 4.8 s); the review runs everything over that link, where the laptop has local SQLite.
- **Causes and fixes:**
  1. **N+1:** `Installation.fx_numbers` reads `self.devices` per row, and `_active_dst_installations` (DST Audit page, Confirm Captures, runner) iterated all 847 active installations without prefetching, at ~28 s per pass. Fix: `prefetch_related("devices")` (`80853ff`), plus a regression test (`audits/test_query_counts.py`, ≤3 queries for 12 installations). **`/audits/dst/`: minutes → 0.9–1.3 s (11 queries).**
  2. **Reconnect per request:** `CONN_MAX_AGE=0` paid ~330 ms per request. Fix: `DB_CONN_MAX_AGE` (default 300 s) with health checks (`80853ff`); safe with transaction pooling since prepared statements are off.
  3. **Live IMS probe on every dashboard view:** `check_ims_connection()` made two HTTPS calls to IMS (~1.5 s). Fix: cached 60 s per process (`d6563b4`). **`/`: 1.6–2.2 s → ~380 ms warm.**
  4. **Full-size images as previews:** preview `<img>` tags pulled ~2.5 MB PNGs from Supabase (server 190–460 ms each, then the transfer). Fix: the `thumb_src` filter serves the ~21 KB stored thumbnail (~100 ms), and a click still opens the full image (`196523c`).
- **After (through gunicorn from a cold restart):** `/audits/` 0.18 s, `/audits/5/` 0.25 s, `/cameras/` 1.1 s (27 queries; still latency-bound), `/audits/dst/` 0.9–1.3 s, `/map/` 1.3 s (folium CPU on 1 vCPU, 3 queries). Container ≤ 180 MiB; IMS healthy throughout.
- **Unexplained one-off:** a single `GET /cameras/` took **81.4 s** server-side right after a redeploy. It did not reproduce in later cold runs (1.1 s). Probably a slow pooler connect (cf. the 4.8 s connect), but unconfirmed: watch the `[HTTP] … ms=` log lines.
- **Operator note (my error):** cleaning up a test session, my filter deleted every `ployd` ModelBackend session, which **signed the user's browser out** of :8443; re-sign-in is needed. Later timing runs deleted only their own session key. Five undecodable sessions in the shared DB (signed with the laptop's key during earlier local test runs) are harmless leftovers.
- **Possible follow-ups if it is still slow in the browser:** move the Supabase project closer to the Linode (32 ms RTT dominates `/cameras/`), or batch `/cameras/`' 27 queries.

### Phase 5 — One-page review per round (gated)
- **Work:** `/audits/review/<round>/`: one row per camera in the round (newest `DST_SITE` capture per installation), showing:
  - identifier, pole, agency, LTI/DE
  - each device (CBW, VNC L1/L2, or DE lanes) with inline thumbnails and capture timestamps
  - OvrC time vs Finished, with drift highlighted
  - failures inline

  Clicking a thumbnail opens it full size in place. Filters: not complete, drift over a threshold, failed. A round picker defaults to the latest round.
- **Accepted when:**
  - A real published round shows every camera from both its LTI and DE runs, with correct devices, timestamps and drift (spot-checked against 3 job detail pages).
  - Only thumbnails load (checked in the network tab).
  - A re-run site shows its newest capture.

### Phase 6 — Shared Complete checkbox + Notes (gated)
- **Work:**
  - An `InstallationAuditReview` model (`round` FK, `installation` FK, `completed`, `completed_by`, `completed_at`, `notes`, `notes_updated_by`, `notes_updated_at`, `version`; unique on (round, installation)). It lives only in the shared DB and is never published over.
  - An inline Complete checkbox per row, with a "Complete x/y" counter.
  - Saves carry `version`, so concurrent edits produce a warning instead of an overwrite.
  - **Any signed-in user can tick Complete and edit Notes** (`readonly+`; user decisions 2026-10-27). Each change records who and when, so open access stays accountable.
  - **Notes UI = a port of IMS's "RA modal"** (the Resolving Actions editor). Source:
    - `IMS/js/index.js:3027–3166`: `ensureRAEditModal`, `openRAEditModal`, `saveAndCloseRAEditModal`, `closeRAEditModal`, and the delegated click handler at `:3169`.
    - `IMS/css/index.css:4370–4460`: `.wo-ra-modal-*` and `#wo-ra-edit-text`; plus `.highlight-success` / `.highlight-error` at `:4328`.

    Copy it into DST (`static/app.css` + a small `static/notes-modal.js`) under DST class names (`dst-notes-modal-*`), keeping IMS's look and behavior:
    - **Markup:** injected into `<body>` once, on first open.
      - Overlay: `position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 9100`, centred, shown by `.is-open`.
      - Dialog: `#0f172a`, 1px `rgba(148,163,184,.2)` border, 12px radius, `max-width: 560px`, `width: 95%`, 24px padding.
      - Header: title "Notes — <identifier / pole>" plus a ✕ button (`#94a3b8`).
      - Body: a `rows="6"` textarea (`#1e293b`, 6px radius, `#e2e8f0` text, vertical resize).
      - Footer: right-aligned **Cancel** and **Save**.
    - **Trigger:** each review row has a notes cell showing the first 120 characters with `…`, or a dim "Click to add notes…" when empty. The full text is kept in `data-notes-value`. Clicking the cell opens the modal.
    - **Open:** load `data-notes-value` into the textarea and focus it with the cursor at the end (the 50 ms `setTimeout`, as in IMS).
    - **Close semantics (same as IMS):** **Save**, **✕** and **clicking the backdrop** all *save and close*. **Cancel** discards. Escape was not bound to this modal in IMS, so Escape = Cancel is a DST addition — confirm or drop it during review.
    - **After save:** update the cell's snippet and `data-notes-value`, then flash `highlight-success` for 1 s; on error, flash `highlight-error` for 2 s. A `version` conflict response shows the warning and keeps the modal text, so nothing typed is lost.
    - DST is already dark-themed (`color-scheme: dark`; `--ice-*` palette in `static/app.css`), so IMS's slate colors fit without re-theming.
- **Accepted when:**
  - A check or note made in one browser shows in a second browser (a different user) after refresh.
  - A `readonly` user can tick Complete and edit notes. The modal's look and behavior match IMS's RA modal: side-by-side screenshot, plus ✕ and backdrop click save while Cancel discards.
  - Concurrent note edits produce the conflict warning.
  - The "not complete" filter respects the checkbox.
  - Re-publishing a round does not reset reviews.

### Final acceptance — a real audit day end to end
LTI-only DST run on the camera VPN → VPN off → DE-only DST run → publish both into one round → reviewers on :8443 see one list, tick Complete and add Notes.

## Hosting — IMS Linode :8443 (evidence 2026-10-27)
- **Box** (read-only check, `deploy@45.79.220.29`):
  - 1 vCPU, 1.9 GiB RAM (463 MiB used, 1.5 GiB available), load 0.37.
  - Disk: 19 GB free of 49 GB.
  - One container, `access_replacement-app-1` (58 MiB), bound to host ports 80 and 443.
  - IMS deploys from `/opt/access_replacement`, using compose file secrets (`./secrets/*` → `/run/secrets/*`).
  - IMS's TLS certificate is **self-signed, `CN=45.79.220.29`**, expiring 2027-04-28.
- **Shape:**
  - A separate compose project at `/opt/dst_camera_audit`.
  - gunicorn serves TLS itself on **8443** with IMS's certificate and key, mounted read-only.
  - `mem_limit: 512m`.
  - File secrets in `/opt/dst_camera_audit/secrets/`.
  - `DST_ROLE=review`.
  - IMS's compose project is not touched.

## Issue digest
- **I1 — resolved 2026-10-27:** Supabase rejected the stored password (a commented `DB_PASSWORD_PRODUCTION` value with an inline comment after it). The user replaced it; auth passes on 6543 and 5432.
- **I2 — resolved by design 2026-10-27:** the camera VPN (OpenVPN, full tunnel) blocks outbound 5432/6543, and IMS IT won't open ports. Resolution: data flow C′ (capture locally, publish with the VPN off). Rejected alternatives: host-route bypass (fragile, may breach VPN policy), Tailscale relay (an extra dependency), HTTPS ingest via the Linode (needs 443, which IMS holds).
- **I3 — open, checked in Phase 4:** the camera VPN only allowed 443 among the ports tested. Reviewers must be able to reach `:8443` from their own networks. If they can't, review moves to 443 behind a reverse proxy shared with IMS.

- **I4 — resolved 2026-10-28:** the user approved committing the CBW plan's schema/sync files on their own (`dbe111e`: `cameras/models.py`, `cameras/migrations/0006_…`, `services/ims_sync.py`; the rest of that plan stays uncommitted). Redeployed: in the container, `Installation` model fields = Supabase columns (**38 = 38**, none missing either way); container healthy at 128 MiB, IMS healthy. Previously **open, blocked the Phase 4 deploy:** the shared DB has `cameras.0006_installation_county_installation_created_at_and_more` applied. It belongs to the **CBW firmware plan** (`in-progress-plans/CBW_FIRMWARE_INVENTORY_REPORT.md`) and was sitting uncommitted in the working tree when Phase 1 ran `migrate --database shared`. It adds 13 NOT NULL `Installation` columns with no DB default (`county`, `street`, `site_type`, `created_at`, …). The committed code (`8b20479`) has no such fields (`cameras/models.py`, the migration and `services/ims_sync.py` are uncommitted), so an image built from a commit would fail every `sync_installations` insert. **Resolution:** commit the CBW plan's schema/sync changes before deploying (the user's call: it's that plan's work), or deploy from a tree that includes them.
- **I5 — open (IMS, outside this plan):** `/opt/access_replacement/certs/key.pem` (IMS's TLS private key) is mode **0666**, world-readable *and* writable. Recommend `chmod 600`; the DST container runs as uid 1000 (`deploy`, the owner), so DST is unaffected by that change.

- **I6 — resolved 2026-10-28:** the user opened 8443 in the Linode Cloud Firewall. From the laptop: `/accounts/login/` **200 in 0.18 s** ("Login — DST Camera Audit", "Sign in with IMS"); the cert is IMS's (`98:DB:…:05:E9`); signed-out `/` and `/media-db/…` → 302 to login; POST start/publish → 403; `/explorer/` → 403; IMS :443 `/healthz` 200. Previously **open, blocked Phase 4 acceptance:** inbound TCP 8443 to `45.79.220.29` is dropped (timeout from outside; the service is healthy on the box). **Resolution (operator):** `sudo ufw allow 8443/tcp` and/or allow 8443 in the Linode Cloud Firewall.

## Open questions
None. (Q1 resolved 2026-10-27: anyone signed in can tick Complete and edit Notes.)

## Checklist
- [x] Phase 0 — network reach spike (VPN off PASS; VPN blocks Postgres; DE needs no VPN)
- [x] I1 — Supabase password fixed
- [x] I2 — VPN blocks Postgres → resolved by the capture-then-publish design
- [x] Phase 1 — roles + shared schema + API roles locked out + reaper off in review (accepted 2026-10-27)
- [x] Phase 2 — stable IDs, audit rounds, structured OvrC time, `lock_shared_db` (accepted 2026-10-27)
- [x] Phase 3 — publish to shared DB (command + panel, can be resumed and re-run) (accepted 2026-10-27)
- [ ] Phase 4 — review instance on the IMS Linode :8443 (incl. I3 reviewer reachability) — local half implemented + verified; deploy pending, not accepted
- [ ] Phase 5 — one-page review per round
- [ ] Phase 6 — shared Complete checkbox + Notes (notes editable by anyone signed in, IMS RA-modal style)
- [ ] Remove the "Acceptance test 2026-10-27" round (shared + local tags) before the first real round
- [x] I4 — CBW plan's cameras.0006 schema committed (`dbe111e`) and redeployed; model = Supabase (38/38 columns)
- [x] I6 — inbound 8443 opened (Linode Cloud Firewall); reachable from the laptop
- [x] Production IMS SSO client `dst_camera_audit_review` + API token installed (2026-10-28); first sync 847 installations
- [ ] Final acceptance — real audit day end to end
- [x] Q1 resolved — anyone signed in can tick Complete and edit Notes

## Status
in progress — 10/15. Phases 0–3 accepted. Phase 4: SSO sign-in works on :8443; the slowness was investigated and fixed (N+1, reconnects, IMS probe, full-size previews). Remaining Phase 4 acceptance: user confirms speed in the browser, and I3 (a reviewer on their own network).

`Superseded by:` —
