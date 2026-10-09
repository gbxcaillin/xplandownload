# Brightly live stack (VPS)

```
internet ──443──> Caddy (HTTPS, automatic certificates)
                    └─> oauth2-proxy (Microsoft Entra sign-in, MFA via Conditional Access)
                          └─> Brightly app ──> PostgreSQL        (internal network, no ports)
vault (client uploads) ── /v/<token> public ── ClamAV scan + AES-256 encryption as files stream in
                        └ /vault/ staff pages, only after sign-in
backup container ──nightly──> pg_dump + vault → checked → age-encrypted → Azure "backups"
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

## Client vault (secure upload links)

Staff open `https://<DOMAIN>/vault/` (Microsoft sign-in), enter the client's name (and Xplan ID
/ family group), and get a link plus a 6-digit code. Send the link by email and the code by SMS
or phone: never both in the same message. The client opens the link, enters the code and drags
files in. Each file is checked by ClamAV and encrypted (AES-256-GCM, its own key, wrapped by
`VAULT_KEY`) while it streams in, so it is never on disk unencrypted. Staff see what arrived,
download, delete, close or reopen links. Every step is in the Activity list (append-only).

- Links expire (default 14 days). Five wrong codes lock a link; staff can reopen it.
- PDFs, photos and Office documents only, up to 100 MB each, 50 per link. Viruses are refused.
- **`VAULT_KEY` must be in the password manager.** Without it the vault's files (and their
  backups) can't be read. Create it with `openssl rand -base64 32`.
- Optional email (`SMTP_*`): the link can be emailed to the client, and staff get a notice
  when files arrive. Without it, staff copy the link themselves.
- Test the virus check after deploying: upload a file containing the standard EICAR test
  string (eicar.org). It must be refused.
- Server folders: `sudo mkdir -p /srv/brightly/vault && sudo chown 10001 /srv/brightly/vault`.
- ClamAV needs about 1.5 GB of RAM and a few minutes after first start to download its
  signatures. Uploads are refused until it is ready.

### AI review of uploads (reviewer container)

When a client presses "I'm finished" (or 30 minutes after their last upload, or when staff press
"Review now"), the reviewer reads the new files and writes **"Suggested client data updates"** as
a PDF into the client's SharePoint folder (`Clients/Active|Inactive/<Name> (<Xplan ID>)`, found by
the Xplan ID entered on the link; otherwise `Clients/_Vault reviews`). The same PDF is on the
vault page. It lists suggested field changes (current value, suggested value, source file and
page, confidence), things needing attention, and a summary of each document. **Nothing in the
client's record changes automatically.**

How it is kept safe:
- Each review is a Claude Agent SDK session that can only `Read`/`Glob` its own job folder (a
  memory-only copy of the decrypted files, wiped afterwards): no shell, no writing, no web.
- Text inside documents is treated as data. Instructions found in a document are flagged, not
  followed.
- Output is structured JSON, then filtered in code: TFNs removed (ATO check digit), long account,
  member and ID numbers cut to the last 4 digits. Health details are kept out of the record sent
  to Claude and are never written; the PDF only flags "health information present".
- Each review has a spending cap (`REVIEW_MAX_USD`, default US$3).

Setup:
1. **Claude.** Either `ANTHROPIC_API_KEY` (Anthropic API: documents are processed offshore,
   mainly in the US, so the privacy policy and client consent must cover that) **or**
   `CLAUDE_CODE_USE_BEDROCK=1` with `AWS_REGION=ap-southeast-2` and keys of an IAM user limited
   to `bedrock:InvokeModel*` (processed in Australia). For Bedrock, set `REVIEW_MODEL` to the
   Bedrock model ID shown in the AWS console.
2. **SharePoint.** In Entra, register an app "Brightly vault reviewer" and give it the Microsoft
   Graph *application* permission **Sites.Selected**, with admin consent. Then grant it write
   access to the one site that holds XPlan Files:
   ```
   POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
   {"roles":["write"],"grantedToIdentities":[{"application":{"id":"<app client id>",
     "displayName":"Brightly vault reviewer"}}]}
   ```
   (Graph Explorer, signed in as a SharePoint admin). Put the tenant ID, client ID and secret in
   `GRAPH_*`, and set `SHAREPOINT_SITE` (e.g. `prosperum.sharepoint.com:/sites/ProsperumTeamFiles`).
3. **Client record (optional).** A read-only database login, `REVIEW_DB_URL`:
   ```sql
   CREATE ROLE reviewer LOGIN PASSWORD '...';
   GRANT CONNECT ON DATABASE brightly TO reviewer;
   GRANT USAGE ON SCHEMA public TO reviewer;
   GRANT SELECT ON family_group, person, contact_point, account, asset_liability, goal TO reviewer;
   ```
   Without it the PDF still lists what the documents say, just without "currently on file".

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
docker compose exec -T db psql -U brightly -d restore_test -c "select count(*) from family_group"
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
