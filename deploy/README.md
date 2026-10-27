# Review instance deploy (IMS Linode, port 8443)

The review instance (`DST_ROLE=review`) shows published DST audits from the
shared Supabase DB. It runs no captures, so selenium, VNC and TeamViewer are not
in the image. It shares the IMS Linode (`deploy@45.79.220.29`) on **port 8443**;
IMS keeps 80/443, and `/opt/access_replacement` is never edited.

```
/opt/dst_camera_audit/
├── docker-compose.yml      ← deploy/docker-compose.yml
├── .env                    ← IMS_SSO_CLIENT_ID=… (compose variable, not secret)
├── app/                    ← source tree (git archive of the release commit)
└── secrets/                ← one value per file, chmod 600, owner deploy
    ├── django_secret_key
    ├── db_user             ← postgres.<project-ref>
    ├── db_password
    ├── ims_api_token       ← scope ase_installations:read
    └── ims_sso_client_secret
```

## One-time prerequisites (operator)

1. **IMS SSO client:** in IMS, Admin → External Integrations, add the redirect URI
   `https://45.79.220.29:8443/accounts/ims/callback/` (to the existing DST client
   or a new one). Put its client id in `/opt/dst_camera_audit/.env` and its
   secret in `secrets/ims_sso_client_secret`.
2. **Firewall:** allow TCP 8443 inbound (ufw on the host, and the Linode Cloud
   Firewall if one is attached).
3. **Directory:** `/opt` is root-owned, so create the project dir once:
   `sudo install -d -o deploy -g deploy -m 750 /opt/dst_camera_audit`.
4. **Key readability:** the container runs as the host `deploy` user (uid 1000,
   which owns `/opt/access_replacement/certs/`), so the mounted `key.pem` stays
   readable at `0600`. As of 2026-10-27 the key is `0666` (world-writable);
   tighten it with `chmod 600 /opt/access_replacement/certs/key.pem`. That is an
   IMS change, so verify IMS afterwards.

## Deploy / update

From the laptop (capture role), at the release commit:

```powershell
git archive --format=tar HEAD | ssh -i $env:USERPROFILE\.ssh\linode_access_replacement deploy@45.79.220.29 "rm -rf /opt/dst_camera_audit/app && mkdir -p /opt/dst_camera_audit/app && tar -x -C /opt/dst_camera_audit/app"
scp -i $env:USERPROFILE\.ssh\linode_access_replacement deploy/docker-compose.yml deploy@45.79.220.29:/opt/dst_camera_audit/
```

On the Linode:

```bash
cd /opt/dst_camera_audit
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
