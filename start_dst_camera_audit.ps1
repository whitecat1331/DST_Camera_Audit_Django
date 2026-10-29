$ErrorActionPreference = "Stop"

# Start DST Camera Audit's Django dev server with the project venv.
# Run from repo root or via: pwsh .\start_dst_camera_audit.ps1 [flags]
#
# Flags pass through to `python manage.py runserver`, for example:
#   pwsh .\start_dst_camera_audit.ps1 --http 8050
#   pwsh .\start_dst_camera_audit.ps1 --http 8050 --ims http://127.0.0.1:8051
#   pwsh .\start_dst_camera_audit.ps1 --https 8050

# Run from the repository root regardless of caller location.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$activate = Join-Path $scriptDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Error "Missing .venv — run: python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt"
}
. $activate

Write-Host "Starting DST Camera Audit (python manage.py runserver)..." -ForegroundColor Cyan
Write-Host "Point at IMS with: --ims <IMS base URL>" -ForegroundColor DarkGray

python manage.py runserver @args
