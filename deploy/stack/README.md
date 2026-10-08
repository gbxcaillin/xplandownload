# Brightly live stack (VPS)

```
internet ──443──> Caddy (HTTPS, automatic certificates)
                    └─> oauth2-proxy (Microsoft Entra sign-in, MFA via Conditional Access)
                          └─> Brightly app ──> PostgreSQL        (internal network, no ports)
backup container ──nightly──> pg_dump → checked → age-encrypted → Azure "backups"
```

Only ports 80/443 (Caddy) are published. The database and app have no route to or from the
internet; only the backup container can reach Azure.

## One-time setup

1. **DNS**: an A record for the Brightly address (e.g. `crm.brightday.com.au`) → `203.57.50.209`.
2. **Entra app registration** (entra.microsoft.com → App registrations → New registration):
   - Name `Brightly`; single tenant; redirect URI (Web) `https://<DOMAIN>/oauth2/callback`.
   - Certificates & secrets → New client secret (12 months; diary the renewal).
   - Copy the tenant id, client id and secret into `.env`.
   - Enterprise applications → Brightly → Properties: *Assignment required* = Yes; then
     Users and groups → add the staff who may use Brightly.
   - Conditional Access (or Security defaults): require MFA for this app.
3. **Backup key** (on your PC, once): `age-keygen -o brightly-backup.key`. Put the whole file
   in the password manager (two people). Only the **public** key (`age1…`) goes in `.env`.
   Delete the key file from the PC afterwards.
4. **Azure backups container**: Shared access token on `backups` with Add, Create, Write, List
   (no Read/Delete), IP `203.57.50.209`, 1-year expiry (diary the renewal) → `AZURE_BACKUP_SAS_URL`.
   Lifecycle rule on the account: blobs under `backups/db/` → delete after 2,557 days; move to
   Cold after 35 days.
5. **On the server**:
   ```bash
   sudo mkdir -p /srv/brightly/pgdata /srv/brightly/backup-spool
   sudo chown -R brightly: /srv/brightly
   cd /opt/brightly            # copy this folder here
   cp .env.example .env && chmod 600 .env && nano .env
   docker compose up -d --build
   docker compose run --rm backup now      # first backup, check it reaches Azure
   ```
6. Open `https://<DOMAIN>`: you are sent to Microsoft sign-in and then see the app (the
   placeholder echoes your request until `BRIGHTLY_IMAGE` is set).

## Loading the Xplan data

From a machine that can reach the database (on the server: `docker compose exec`, or a
one-off container on the `internal` network), with the export folder copied over:

```bash
python -m xplan_extract load --db postgresql+psycopg://brightly:<pw>@db:5432/brightly \
    --export /path/to/export --create-schema --dry-run     # stage + validate only
python -m xplan_extract load --db ... --export /path/to/export          # load
```

## Restore drill (monthly, and before go-live)

```bash
# 1. download the newest backups/db/YYYY/MM/brightly-*.dump.age from Azure (portal or azcopy)
# 2. decrypt with the private key from the password manager
age -d -i brightly-backup.key brightly-YYYYMMDD-HHMM.dump.age > restore.dump
# 3. restore into a scratch database and compare counts with the live one
docker compose exec -T db createdb -U brightly restore_test
docker compose exec -T db pg_restore -U brightly -d restore_test --no-owner < restore.dump
docker compose exec -T db psql -U brightly -d restore_test -c "select count(*) from household"
docker compose exec -T db dropdb -U brightly restore_test
```
Record the date and result; delete `restore.dump` and the key file afterwards.

## Monitoring (step 4.5)

- `/srv/brightly/backup-spool/LAST_SUCCESS` holds the time of the last good backup; alert if
  it's older than 26 hours.
- An external uptime check on `https://<DOMAIN>/ping` (oauth2-proxy answers it without sign-in).

## Moving to Azure later

Push the app image to Azure Container Registry → Container Apps or App Service; create Azure
Database for PostgreSQL (Australia East) and restore the latest backup into it; keep the same
Entra app registration (add the new redirect URI); point DNS at the new address. The backups
container and its history stay where they are.
