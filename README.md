# xplandownload

Downloads the Iress/Xplan data extract from the Iress MFT (SFTP) account and converts
**every table** in it to **Excel** and **JSON**.

The extract Iress provides is a password-protected `.zip` containing a **SQL Server
database backup (`.bak`)**. It isn't a spreadsheet, so the tool:

1. **Downloads** the zip from `mft.iress.com.au` over SFTP (port 22). This replaces FileZilla.
2. **Unzips** it with the zip password from the encrypted email (AES or ZipCrypto).
3. **Restores** the `.bak` into a SQL Server you run locally (SQL Server Express, Developer, or Docker).
4. **Exports** every table to:
   - `excel/<database>.xlsx`: an **Index** sheet (table, row count, link to the sheet,
     JSON file, notes) plus one sheet per table
   - `json/<schema>.<table>.json`: one JSON array of row objects per table
   - `manifest.json`: every table with its columns, SQL types, row counts and file names

```
output/<database>_<timestamp>/
├── excel/<database>.xlsx
├── json/dbo.<table>.json  ...
└── manifest.json
```

## 1. One-time setup

You need **Python 3.10+**, the **Microsoft ODBC Driver 18 for SQL Server**, and a **SQL Server**.

**Windows (most common):**
1. Install Python from https://www.python.org/downloads/ (tick "Add to PATH").
2. Install [SQL Server 2022 Express or Developer](https://www.microsoft.com/sql-server/sql-server-downloads)
   (free). Express has a 10 GB database limit. If the backup is bigger, use Developer.
3. Install [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).
4. In this folder:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

**Alternative: SQL Server in Docker (Windows/Mac/Linux):** run `docker compose up -d` in this
folder. Then set `BACKUP_SHARE_LOCAL` / `BACKUP_SHARE_SERVER` as in `.env.example` so the
container can read the backup.

## 2. Put the details from the Iress email in `.env`

Copy `.env.example` to `.env` and fill in the values. `.env` is git-ignored. **Never commit it.**

```
MFT_USERNAME=au-dm-XXXX
MFT_PASSWORD=...
MFT_REMOTE_FILE=extract_xxx_DMXXXX_YYYYMMDDHHMM.zip
ZIP_PASSWORD=...
```

SQL Server connection:
- SQL Server Express with Windows login: `SQL_SERVER=.\SQLEXPRESS`, and leave `SQL_USER` empty.
  Remove the `BACKUP_SHARE_*` lines.
- Docker: keep the values from `.env.example` (`SQL_USER=sa`, `SQL_PASSWORD` = the compose password).

If you'd rather not store the passwords, leave them empty and you'll be prompted.

## 3. Run it

```
python -m xplan_extract run --accept-new-host-key
```

`--accept-new-host-key` is only needed the first time. It trusts the Iress server's SSH key
and saves it to `known_hosts`. The fingerprint is printed so you can check it.

Already downloaded the zip with FileZilla? Skip the download step:

```
python -m xplan_extract run --zip "C:\Users\me\Desktop\Data Extract\extract_xxx.zip"
```

### Large databases: look before you export

```
python -m xplan_extract tables
```
lists every table with its row count, size and binary columns (stored documents/images), and
saves the full list to `output/tables_<database>.csv`. Use it to choose what to export, e.g.

```
python -m xplan_extract export --no-binary --exclude "*audit*" --exclude "*log*"
```

### Stored documents (PDFs, Word, emails …) to a folder / SharePoint

```
python -m xplan_extract documents --dest "C:\Users\me\Company\Team Files - General\XPlan Files" --dry-run
python -m xplan_extract documents --dest "..." --limit 20      # trial: first 20 file notes
python -m xplan_extract documents --dest "..."                 # everything
```

Writes `Clients/<Client name> (<id>)/<date> <type> - <file>` for every attached file, an
`.html` copy of each file note (date, type, subject, clients, note text, links to its
attachments), `Other attachments/…` for `_attachmentdata`, and `documents_index.csv` listing
everything. A note linked to several clients (a couple, a family trust) is saved, with its
attachments, in each of those clients' folders.

For a OneDrive-synced SharePoint folder (Files On-Demand on), each file is marked
online-only so OneDrive frees the local copy after uploading, and the export pauses while
free disk space is below `--min-free-gb` (default 10). Re-running skips files already saved.

### Active / Inactive client folders and the clients to confirm

```
python -m xplan_extract confirm-list                 # refresh active_clients_to_confirm.xlsx
python -m xplan_extract split-folders                # preview: writes folder_split_preview_*.csv
python -m xplan_extract split-folders --apply        # move the folders
```

Both read the fees-by-client report (`BRIGHTLY_ACTIVE_LIST`) and the restored database.

`confirm-list` rebuilds `active_clients_to_confirm.xlsx` next to the fee report, keeping
whatever is already typed in the *Confirmed Xplan ID* and *Notes* columns (the old file is
kept as `…backup-<date>.xlsx`). A **Status** column shows each client as answered,
waiting, or needing a check. Answers are Xplan IDs (a comma between two) or "not in Xplan".

`split-folders` moves each `Clients/<name> (<id>)` folder into `Clients/Active` or
`Clients/Inactive`. A client is Active when they or their family group match the fee list,
including Scott's answers. Folders are identified through `documents_index.csv`, which is
updated to the new paths (a copy of the old one is kept). Folders starting with `_` stay
where they are. Re-running is safe: after more answers come in, folders move back as
needed. Inside a synced SharePoint folder a move is a rename, so nothing is downloaded or
re-uploaded.

### Duplicate client folders (same person, two Xplan records)

```
python -m xplan_extract merge-folders            # writes folder_merge_review_<date>.xlsx
python -m xplan_extract merge-folders --apply    # merges the rows marked "merge"
python -m xplan_extract merge-folders --apply --only high   # just the High-confidence rows
```

Folders are grouped when their names match ignoring spaces, case and punctuation
("Mc Donough" = "Mcdonough"), or when the clients share a surname and date of birth. Each
extra folder is checked against the group's main folder (Active first, then the most files)
using Xplan: the same date of birth, email, phone or address count as evidence, as do the same
documents filed under both. Different dates of birth, first or middle names, a person vs a
trust, or the two being partners count as conflicts. Conflicts are pre-filled "keep" and
everything else "merge"; change the Decision column before applying.

Merging moves the files into the main folder. A file that is the very same Xplan document as
one already there is removed (it goes to the SharePoint recycle bin), and a different file with
the same name becomes "<name> (from <id>)". `documents_index.csv` follows every move, and
`folder_merge_done_<date>.csv` logs what was merged. The Brightly records stay separate until
they are merged with `merge-ui` after loading.

### Archive to Azure Blob storage

Keeps an encrypted, checked copy of the raw Iress zip (and, separately, the exports) in Azure.
Needs [7-Zip](https://www.7-zip.org) and [AzCopy](https://aka.ms/downloadazcopy-v10-windows)
(set `AZCOPY` / `SEVEN_ZIP` in `.env` if they aren't on PATH) and the
**Storage Blob Data Contributor** role on the storage account.

```
python -m xplan_extract archive --set raw        # the Iress zip -> xplan-raw (Cold tier)
python -m xplan_extract archive --set derived    # OUTPUT_DIR exports -> xplan-derived
```

If the Iress zip and `.bak` have been deleted, make a fresh, verified backup of the restored
database first and archive that:

```
python -m xplan_extract backup --dest E:\                      # COPY_ONLY, compressed, checksummed, verified
python -m xplan_extract archive --set raw --file E:\<database>.bak
```

Each run writes `MANIFEST-<set>.json` (SHA-256 of every source file), packs it with the files
into a 7-Zip AES-256 archive (7-Zip asks for the password; keep it in the password manager),
tests the archive, uploads it with AzCopy (sign in with your Microsoft account) and checks
Azure's MD5 against the local one. `ARCHIVE_INDEX.json` in the staging folder records what was
archived. Re-running skips finished steps.

### Brightly database (draft schema and loader)

The database choice (PostgreSQL or Azure SQL) is still Scott's call, so nothing is created
unless you ask. The same schema works on both.

```
python -m xplan_extract schema-sql --dialect postgresql --out schema.postgresql.sql   # review
python -m xplan_extract schema-sql --dialect mssql      --out schema.mssql.sql
python -m xplan_extract load --db <url> --export <export folder> --create-schema --dry-run
python -m xplan_extract load --db <url> --export <export folder>
```

`load` stages every record, checks counts against `manifest.json`, ids, links, dates and
TFNs, and only then loads in one transaction. Re-running updates records by id; rows added in
Brightly afterwards (`source = 'brightly'`) are kept, and records edited in Brightly since the
last import are left alone unless `--overwrite-edited` is given. The server stack that hosts
the database is in `deploy/stack/`.

### Staged roll-in: Active clients first

```
python -m xplan_extract brightly --phase active          # phase 1: export_<date>_active
python -m xplan_extract load --export "...\export_<date>_active" --dry-run
python -m xplan_extract load --export "...\export_<date>_active"
# later, phase 2: everything, loaded on top
python -m xplan_extract brightly
python -m xplan_extract load --export "...\export_<date>"
```

Phase 1 holds only the Active family groups (from the fee list and Scott's answers), their
file notes, advice history and tasks, and the SMSFs, trusts and companies they belong to.
An entity shared with an inactive family group carries only its active links for now. If its
home family group is inactive, it points at its active family group until phase 2. Add
`--include-prospects` to bring prospects in with phase 1.

Phase 2 is a normal full export. Loading it adds the inactive family groups and the remaining
links. Records already edited in Brightly are skipped (`--overwrite-edited` to force), so work
done since phase 1 is kept.

### Ongoing fee consents, change history and locked advice records

```
python -m xplan_extract ofa seed                    # one arrangement per Active client with a fee
python -m xplan_extract ofa worklist                # Excel: lapsed, urgent, due soon, open ...
python -m xplan_extract ofa form OFA-H-12345-1      # Word consent form for e-signing
python -m xplan_extract ofa consent OFA-H-12345-1 --signed 2026-11-03 --method esign
python -m xplan_extract records lock "SOA.pdf" --client H-12345 --kind soa
python -m xplan_extract records history H-12345
```

The consent rules follow ASIC INFO 286. The window runs from 60 days before to 150 days after
each anniversary. Without consent in the window the arrangement ends, and the account providers
must be told within 10 business days (the count skips Victorian public holidays). A written
withdrawal must be acknowledged, any later fees refunded and the providers told within 10
business days. Every step is kept in `ofa_event`, which can't be edited, for at least 5 years.
Arrangements created from Xplan start flagged "confirm the last consent", because Xplan's
consent history isn't in the extract. Where an Xplan file note looks like a consent, its date
is offered as a hint.

On PostgreSQL every change to a client record is written to `audit_log` (who, when, old and new
values). The app names the person with `audit.set_actor(conn, email)`. Imports are recorded once,
not row by row. `records lock` copies a final document to the Azure container `advice-records`
with a **locked** 7-year retention policy, so it can't be changed or deleted, by anyone, until
then. Its SHA-256 fingerprint is kept for checking copies.

### Duplicate family groups and entities (merge)

Brightly calls a household a **family group**. When the same family group or entity exists
twice, open the review page and merge them:

```
python -m xplan_extract merge-ui --db <url>         # then open http://127.0.0.1:8765
```

It lists likely duplicates (same name, email, phone, surname + date of birth, or ABN) and lines
the two records up field by field, including each person and each fact-find answer. By default
a filled value beats a blank one and, on a conflict, the record you keep wins; change any
field's pick, or tick **Exclude** to keep the kept record's value as it is. Everything attached
to the other record moves across, the merge is logged, and a later re-import from Xplan won't
recreate the duplicate.

### Individual steps

```
python -m xplan_extract download                  # SFTP download only -> data/download
python -m xplan_extract unzip  data/download/x.zip
python -m xplan_extract info   data/download/x.zip   # what's inside and how big unzipped
python -m xplan_extract restore data/extracted/x/backup.bak [--check-only] [--replace]
python -m xplan_extract tables                    # table sizes in the restored database
python -m xplan_extract export                    # export the restored database
```

### Export options

| Option | Effect |
|---|---|
| `--excel-layout per-table` | one `.xlsx` per table instead of one workbook |
| `--json-layout single` / `both` | one combined `<database>.json` (`{"tables": {"dbo.x": [...]}}`) / both |
| `--jsonl` | per-table JSON Lines (one row per line; better for very large tables) |
| `--include-empty` | also create sheets/files for tables with 0 rows (they're always listed in the Index/manifest) |
| `--include-views` | export views too |
| `--schema S`, `--table T` | only some schemas/tables (repeatable, wildcards like `client*` allowed) |
| `--exclude T` | skip tables (repeatable, wildcards allowed) |
| `--no-binary` | leave out binary columns (stored documents/images) |
| `--no-excel`, `--no-json` | skip one format |

## Things to know about the output

- **Excel limits are handled.** A table with more than 1,048,575 rows continues on extra sheets
  (`table (2)`, `table (3)`, …). Text longer than 32,767 characters is truncated in Excel and
  flagged on the Index sheet. The full value is always in the JSON.
- Text is always written as text, so values like `=SUM(...)` or URLs are never turned into
  formulas or links.
- **JSON:** dates/times are ISO 8601 strings, binary columns (e.g. stored documents) are base64,
  and decimals that a JSON number can't hold exactly are strings.
- Excel shows binary columns as `0x…` (≤16 bytes) or `<binary N bytes>`.
- SQL Server `geography`/`geometry` become WKT text, and `hierarchyid` becomes its path string.
- If one table fails, the export continues. The error is in the Index sheet and in `manifest.json`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Authentication failed` | Copy the username/password again with no spaces. The MFT request closes after 2 weeks. |
| `Could not connect … timed out` | Your public IP isn't whitelisted by Iress, or a firewall/antivirus blocks port 22. |
| `server … is not yet trusted` | First connection: add `--accept-new-host-key`. |
| `Wrong password for …zip` | Use the *unzip* password from the email, not the MFT password. |
| `SQL Server could not read the backup` | The SQL Server service account must be able to read the `.bak`. Put it in a folder like `C:\SQLBackups`, or use the `BACKUP_SHARE_*` settings for Docker. The SQL Server version must be the same as or newer than the one that made the backup. |
| `No SQL Server ODBC driver found` | Install ODBC Driver 18 (see setup). |

## Tests

```
pip install pytest openpyxl
python -m pytest
```
