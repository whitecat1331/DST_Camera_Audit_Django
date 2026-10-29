# Review instance deploy (IMS Linode, port 8443)

The review instance (`DST_ROLE=review`) shows published DST audits from the
shared Supabase DB. It runs no captures, so selenium, VNC and TeamViewer are not
in the image. It shares the IMS Linode (`deploy@45.79.220.29`) on **port 8443**;
IMS keeps 80/443, and `/opt/access_replacement` is never edited.

```
/home/deploy/dst_camera_audit/      (~deploy; no sudo needed)
├── docker-compose.yml      ← deploy/docker-compose.yml
├── .env                    ← IMS_SSO_CLIENT_ID=…, DB_HOST=db.<project-ref>.supabase.co (compose variables)
├── app/                    ← source tree (git archive of the release commit)
└── secrets/                ← one value per file, chmod 600, owner deploy
    ├── django_secret_key
    ├── db_user             ← postgres (direct connection; db_user.pooler keeps postgres.<project-ref>)
    ├── db_password
    ├── ims_api_token       ← scope ase_installations:read
    └── ims_sso_client_secret
```

## One-time prerequisites (operator)

1. **IMS SSO client:** in IMS, Admin → External Integrations, add the redirect URI
   `https://45.79.220.29:8443/accounts/ims/callback/` (to the existing DST client
   or a new one). Put its client id in `~/dst_camera_audit/.env` and its
   secret in `secrets/ims_sso_client_secret`. Credentials must come from
   **production** IMS: the laptop's `.env` points at dev IMS (127.0.0.1:8051).
   Don't run `IMS/cmd/mint-dst-integration` against production: its upsert
   overwrites any existing `dst_camera_audit` client's secret and redirect URIs.
2. **Firewall:** allow TCP 8443 inbound (ufw on the host, and the Linode Cloud
   Firewall if one is attached).
3. **Key readability:** the container runs as the host `deploy` user (uid 1000,
   which owns `/opt/access_replacement/certs/`), so the mounted `key.pem` stays
   readable at `0600`. As of 2026-10-27 the key is `0666` (world-writable);
   tighten it with `chmod 600 /opt/access_replacement/certs/key.pem`. That is an
   IMS change, so verify IMS afterwards.

## Database connection (why it isn't the pooler)

The review instance connects **directly** to Postgres (`db.<ref>.supabase.co:5432`,
IPv6), not through Supavisor. From this Linode, about 1 in 8 Supavisor
connections (both nodes, both modes, any reply size) stopped receiving replies
for 17 s to minutes while Postgres had already finished the query. The direct
host had no stalls (plan I7). The direct host is IPv6-only and Docker's bridge
network has no IPv6, so the container uses `network_mode: host` and gunicorn
binds `:8443` on the host itself. The laptop still publishes through the pooler.

**Extract the source with a normal umask** (`umask 022`). Under `umask 077`,
Docker `COPY` keeps the `0700`/`0600` modes and the uid 1000 container can't
import `config` (that crash-looped the container once).

## Deploy / update

From the laptop (capture role), at the release commit:

```powershell
git archive --format=tar HEAD | ssh -i $env:USERPROFILE\.ssh\linode_access_replacement deploy@45.79.220.29 "rm -rf ~/dst_camera_audit/app && mkdir -p ~/dst_camera_audit/app && tar -x -C ~/dst_camera_audit/app"
scp -i $env:USERPROFILE\.ssh\linode_access_replacement deploy/docker-compose.yml deploy@45.79.220.29:dst_camera_audit/
```

On the Linode:

```bash
cd ~/dst_camera_audit
docker compose build && docker compose up -d
docker compose ps                                   # healthy
docker stats --no-stream                            # dst under 512 MiB; IMS unchanged
docker compose exec dst python manage.py sync_installations   # Installation rows from IMS
```

## Schema changes

The review instance **never migrates**. Schema changes are applied from the
laptop with the VPN off, and the lockdown is re-applied every time:

```powershell
$env:DB_PORT=5432; .\.venv\Scripts\python.exe manage.py migrate --database shared
.\.venv\Scripts\python.exe manage.py lock_shared_db
```

Deploy the matching code to the Linode afterwards.
