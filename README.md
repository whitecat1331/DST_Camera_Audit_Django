# DST Camera Audit

Django web app for **ASE installation camera audits** at Blue Line Solutions. Operators sign in through **IMS SSO**, sync installation inventory from IMS, view sites on a Folium map, and run automated capture jobs that store screenshots and metadata locally.

## Capture modes

| Site type | Stack | Env / inputs |
|-----------|--------|----------------|
| **LTI / pole (CBW + VNC)** | Chrome (Selenium) for CBW date/time relay UI; VNC for camera layers | `CBW_USERNAME`, `CBW_PASSWORDS`, `TF_VNC_PASSWORD` |
| **DragonEye** | TeamViewer to DragonCam; FX serial → TeamViewer ID map; **OvrC** local date/time + WattBox **DCAM System** Turn On (only if OFF) | `TV_USERNAME`, `TV_PASSWORD` (comma list: try each for TV connect; **last** entry is the in-session camera login), optional `TEAMVIEWER_PASSWORDS`, CSV upload on dashboard or `DragonEye Teamviewer IDs.csv` in project root (gitignored); `OVRC_USERNAME`, `OVRC_PASSWORD` |

### DST Audit (fleet)

`/audits/dst/` orchestrates a full DST timezone audit for **selected** sites (checkboxes on the eligible list; Select all / LTI only / DE only helpers). Selection persists across searches.

1. **Power on** — all selected sites first (LTI CBW relays in parallel; **one** OvrC login for every DE DCAM outlet)
2. **Settle** — wait (`DST_POWER_SETTLE_SECONDS`, default 90s); skipped if everything was already on
3. **Capture** — per site: LTI CBW + VNC, or DE TeamViewer lanes
4. **OvrC times** — one OvrC pass for all FX local times (keeps the portal’s ADT/EDT zone label)

Compare **OvrC device time** on each site job to **Finished** (EST). Progress bars (4 phases) and cancel are on the DST Audit page; terminal logs use `[DST]` / `[AUDIT]`.

Chrome is required for CBW and OvrC captures. TeamViewer must be installed for DragonEye (`TEAMVIEWER_PATH` optional).

### Export Post Install Images (for IMS)

After a successful **TV Capture** on a DragonEye site detail page, use
**Export Post Install Images** to download a zip that includes:

- TeamViewer lane captures (`tv/de_l1.png`, `tv/de_l2.png`, …)
- Site photos from `{Site Documents}\{pole}\Site Photos\`

Upload that zip in IMS under **Installation Registry → DE Post-Install Pack**.
IMS does not need SharePoint access — everything comes from this zip.

## Setup

```powershell
cd DST_Camera_Audit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env with your values (never commit .env)
```

In the **PatsDev** multi-root workspace, new PowerShell terminals auto-activate `.venv` when present (see `.vscode/settings.json`).

### IMS prerequisites

On IMS (**Admin → External Integrations**), create:

1. **API token** with scope `ase_installations:read`
2. **SSO client** with redirect URI `http://127.0.0.1:8000/accounts/ims/callback/` (or your DST public URL)

Apply IMS migration `055_api-tokens-sso.sql` before SSO.

### Environment variables

See `.env.example` for the full list. Required for production use:

- **Django:** `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`
- **IMS:** `IMS_BASE_URL`, `IMS_SSO_CLIENT_ID`, `IMS_SSO_CLIENT_SECRET`, `IMS_API_TOKEN`
- **LTI captures:** `CBW_USERNAME`, `CBW_PASSWORDS`, `TF_VNC_PASSWORD`
- **DragonEye:** `TV_USERNAME`, `TV_PASSWORD` (connect tries each comma-separated value; camera login uses the last)
- **OvrC (FX local time):** `OVRC_USERNAME`, `OVRC_PASSWORD` (optional `OVRC_BASE_URL`)

Optional: `IMS_SSO_REDIRECT_URI`, `IMS_TLS_VERIFY=false` (dev only), `DST_LOCAL_ADMIN=1` (break-glass Django admin), `AUDIT_MAX_CONCURRENT`, `AUDIT_STEP_CONCURRENT`, `DST_POWER_SETTLE_SECONDS`, `LOG_LEVEL`, `LOG_FILE`, `LOG_TO_FILE`, `TEAMVIEWER_PATH`.

### Logging

Bracket prefixes match sibling apps (`[INIT]`, `[HTTP]`, `[AUTH]`, `[IMS]`, `[SYNC]`, `[AUDIT]`, `[DST]`, `[CBW]`, `[VNC]`, `[TV]`, `[DE]`, `[OVRC]`). Default log file: `logs/dst.log` (rotating; directory is gitignored).

## Run

```powershell
python manage.py migrate
python manage.py sync_installations
python manage.py runserver
```

Open http://127.0.0.1:8000/ and **Sign in with IMS**.

| IMS role | Access |
|----------|--------|
| `readonly+` | View dashboard, map, audits |
| `technician+` | Start audit jobs |
| `admin+` | Sync inventory from IMS |

## Routes

| Path | Purpose |
|------|---------|
| `/` | Dashboard + sync from IMS (admin+) |
| `/cameras/` | Installation cards with device/sensor layers and capture thumbnails |
| `/cameras/<id>/` | Detail, audit controls, **Export Post Install Images** (DE zip for IMS) |
| `/cameras/<id>/export-de-pi-tv/` | Download Site Documents + TV captures as `*_post_install_images.zip` |
| `/map/` | Folium map |
| `/audits/`, `/audits/<id>/` | Job list and status + screenshots |
| `/audits/dst/` | DST Audit orchestration (power-all → settle → capture → OvrC times) |
| `/sync/` | POST re-sync from IMS |
| `/accounts/ims/start/`, `/accounts/ims/callback/` | IMS SSO |

## Git workflow

Same pattern as IMS / PatsPrints / PatsScraper:

1. Develop on local **`feature`** only (do **not** push `feature`).
2. Merge `feature` → **`dev`**, push `dev`.
3. Open PR **`dev` → `main`** (metadata in `.github/release-pr.json`; use the `release-pr` skill after atomic commits).

CI (`.github/workflows/ci.yml`) runs `manage.py check` and `manage.py test` on pushes to `dev` and PRs targeting `dev` or `main`.

## Notes

- Inventory: IMS `GET /api/external/ase-installations?include=devices` (devices, sensors, components).
- Sync stores devices/sensors locally; cards match CBW / VNC L1 / VNC L2 (and other hosts).
- Audit CBW/VNC targets use pole-derived IPs (`services/ip_map.py`).
- After IMS or DST upgrades, restart IMS and run **Sync from IMS** (or `python manage.py sync_installations`).
