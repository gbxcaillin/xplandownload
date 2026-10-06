"""Command line interface.

    python -m xplan_extract run        # download -> unzip -> restore -> export (all steps)
    python -m xplan_extract download   # only download from the Iress MFT account
    python -m xplan_extract unzip ZIP  # only extract the password-protected zip
    python -m xplan_extract restore BAK
    python -m xplan_extract export --database NAME
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import archive, exporter, sftp, sqlserver


def log(msg: str) -> None:
    print(msg, flush=True)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str) -> bool:
    return (env(name, "") or "").strip().lower() in ("1", "true", "yes", "y")


def secret(value: str | None, prompt: str) -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"Missing {prompt} (set it in .env or pass it as an option).")
    return getpass.getpass(f"{prompt}: ")


# --------------------------------------------------------------------------
# Argument groups
# --------------------------------------------------------------------------

def add_mft_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("Iress MFT (SFTP) account")
    g.add_argument("--host", default=env("MFT_HOST", "mft.iress.com.au"))
    g.add_argument("--port", type=int, default=int(env("MFT_PORT", "22")))
    g.add_argument("--username", default=env("MFT_USERNAME"))
    g.add_argument("--password", default=env("MFT_PASSWORD"),
                   help="Prefer MFT_PASSWORD in .env (or leave empty to be prompted).")
    g.add_argument("--remote-dir", default=env("MFT_REMOTE_DIR", "/"))
    g.add_argument("--remote-file", default=env("MFT_REMOTE_FILE", "*"),
                   help="File name or wildcard to download (default: everything).")
    g.add_argument("--known-hosts", type=Path, default=Path(env("MFT_KNOWN_HOSTS", "known_hosts")))
    g.add_argument("--accept-new-host-key", action="store_true",
                   help="Trust and remember the server's host key on first connection.")


def add_zip_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--zip-password", default=env("ZIP_PASSWORD"),
                   help="Prefer ZIP_PASSWORD in .env (or leave empty to be prompted).")


def add_sql_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("SQL Server used to restore the backup")
    g.add_argument("--sql-server", default=env("SQL_SERVER", "localhost,1433"),
                   help=r"e.g. localhost,1433  or  .\SQLEXPRESS")
    g.add_argument("--sql-user", default=env("SQL_USER"),
                   help="SQL login (omit to use Windows authentication).")
    g.add_argument("--sql-password", default=env("SQL_PASSWORD"))
    g.add_argument("--sql-driver", default=env("SQL_DRIVER"),
                   help="ODBC driver name (auto-detected by default).")
    g.add_argument("--backup-share-local", type=Path,
                   default=Path(env("BACKUP_SHARE_LOCAL")) if env("BACKUP_SHARE_LOCAL") else None,
                   help="Folder on this machine that SQL Server sees as --backup-share-server "
                        "(needed when SQL Server runs in Docker or on another machine).")
    g.add_argument("--backup-share-server", default=env("BACKUP_SHARE_SERVER"),
                   help="The same folder as seen by SQL Server, e.g. /var/opt/mssql/backups")


def add_restore_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--database-name", help="Restore under this name (default: name in backup).")
    p.add_argument("--replace", action="store_true", help="Overwrite an existing database.")
    p.add_argument("--data-dir", help="Folder (on the SQL Server) for the restored data files.")
    p.add_argument("--log-dir", help="Folder (on the SQL Server) for the restored log file.")


def add_export_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("Export")
    g.add_argument("--out", type=Path, default=Path(env("OUTPUT_DIR", "output")),
                   help="Output folder (a timestamped sub-folder is created).")
    g.add_argument("--no-excel", action="store_true")
    g.add_argument("--no-json", action="store_true")
    g.add_argument("--excel-layout", choices=["single", "per-table"], default="single",
                   help="One workbook with a sheet per table (default) or one workbook per table.")
    g.add_argument("--json-layout", choices=["per-table", "single", "both"], default="per-table",
                   help="One JSON file per table (default), one combined JSON file, or both.")
    g.add_argument("--jsonl", action="store_true", help="Per-table files as JSON Lines.")
    g.add_argument("--include-empty", action="store_true",
                   help="Also create sheets/files for tables with no rows.")
    g.add_argument("--include-views", action="store_true")
    g.add_argument("--schema", action="append", dest="schemas", help="Only these schemas.")
    g.add_argument("--table", action="append", dest="tables",
                   help="Only these tables (name or schema.name, wildcards like client* "
                        "allowed). Repeatable.")
    g.add_argument("--exclude", action="append",
                   help="Skip these tables (wildcards allowed). Repeatable.")
    g.add_argument("--no-binary", action="store_true",
                   help="Leave out binary columns (stored documents/images).")
    g.add_argument("--batch-size", type=int, default=5000)


def sql_config(a: argparse.Namespace) -> sqlserver.SqlServerConfig:
    password = a.sql_password
    if a.sql_user and not password:
        password = secret(None, "SQL Server password")
    return sqlserver.SqlServerConfig(
        server=a.sql_server, user=a.sql_user, password=password,
        trusted_connection=not a.sql_user, driver=a.sql_driver,
    )


def export_options(a: argparse.Namespace) -> exporter.ExportOptions:
    return exporter.ExportOptions(
        excel=not a.no_excel, json=not a.no_json, excel_layout=a.excel_layout,
        json_layout=a.json_layout, jsonl=a.jsonl, include_empty=a.include_empty,
        include_views=a.include_views, schemas=a.schemas, tables=a.tables,
        exclude=a.exclude, skip_binary=a.no_binary, batch_size=a.batch_size,
    )


def output_dir(base: Path, label: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w.-]", "_", label)
    return base / f"{safe}_{stamp}"


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def step_download(a) -> list[Path]:
    username = a.username or (input("MFT username: ").strip() if sys.stdin.isatty() else None)
    if not username:
        raise SystemExit("Missing MFT username (MFT_USERNAME in .env or --username).")
    return sftp.download(
        a.host, a.port, username, secret(a.password, "MFT password"),
        dest_dir=a.download_dir, remote_dir=a.remote_dir, pattern=a.remote_file,
        known_hosts=a.known_hosts, accept_new_host_key=a.accept_new_host_key, progress=log,
    )


def step_unzip(zip_path: Path, a) -> list[Path]:
    dest = a.extract_dir / zip_path.stem
    log(f"Extracting {zip_path} -> {dest}")
    pw = a.zip_password
    files = archive.extract_zip(zip_path, dest, pw, progress=log)
    return files


def step_restore(bak: Path, a) -> str:
    cfg = sql_config(a)
    server_path = sqlserver.server_path_for(bak, a.backup_share_local, a.backup_share_server, log)
    return sqlserver.restore_backup(
        cfg, server_path, database_name=a.database_name, replace=a.replace,
        data_dir=a.data_dir, log_dir=a.log_dir, progress=log,
        check_only=getattr(a, "check_only", False),
    )


def step_export(engine, label: str, a) -> Path:
    out = output_dir(a.out, label)
    log(f"Exporting to {out}")
    manifest = exporter.export_database(engine, out, label, export_options(a), progress=log)
    log("")
    log(f"Done: {manifest['table_count']} table(s), {manifest['total_rows']:,} row(s) "
        f"in {manifest['duration_seconds']}s")
    if manifest["excel_workbook"]:
        log(f"  Excel:    {out / manifest['excel_workbook']}")
    elif not a.no_excel:
        log(f"  Excel:    {out / 'excel'}")
    if not a.no_json:
        log(f"  JSON:     {out / 'json'}")
    log(f"  Manifest: {out / 'manifest.json'}")
    if manifest["failed_tables"]:
        log(f"  WARNING: {len(manifest['failed_tables'])} table(s) failed: "
            + ", ".join(manifest["failed_tables"]))
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_download(a) -> None:
    for p in step_download(a):
        log(f"Downloaded: {p}")


def cmd_unzip(a) -> None:
    a.zip_password = a.zip_password or secret(None, "Zip password")
    for p in step_unzip(a.zip, a):
        log(f"Extracted: {p}")


def cmd_info(a) -> None:
    total = 0
    for name, size in archive.list_contents(a.zip):
        total += size
        log(f"  {name}: {size / 1024 ** 3:,.2f} GB")
    log(f"Zip file: {a.zip.stat().st_size / 1024 ** 3:,.2f} GB; "
        f"unzipped total: {total / 1024 ** 3:,.2f} GB")


def cmd_restore(a) -> None:
    step_restore(a.bak, a)


def cmd_export(a) -> None:
    if a.url:
        import sqlalchemy as sa
        engine = sa.create_engine(a.url)
        label = engine.url.database or "database"
        label = Path(label).stem if engine.dialect.name == "sqlite" else label
    else:
        label = pick_database(a)
        engine = sql_config(a).engine(label)
    step_export(engine, label, a)


def pick_database(a) -> str:
    """The --database given, or the only restored database on the server."""
    if a.database:
        return a.database
    names = sqlserver.user_databases(sql_config(a))
    if len(names) == 1:
        log(f"Using database '{names[0]}'")
        return names[0]
    raise SystemExit("Pass --database NAME. Databases on this server: "
                     + (", ".join(names) or "none (restore the backup first)"))


def cmd_documents(a) -> None:
    from . import documents

    if a.shared_examples:
        from . import documents as docs
        docs.shared_note_examples(sql_config(a).engine(pick_database(a)), a.shared_examples, log)
        return
    if not a.dest:
        raise SystemExit("Pass --dest FOLDER (or set DOCUMENTS_DEST in .env), e.g. your synced "
                         "SharePoint folder.")
    if a.limit and not 0 < a.limit <= 2000:
        raise SystemExit("--limit must be between 1 and 2000.")
    db = pick_database(a)
    active_ids = None
    if a.active_list:
        from .brightly.active import compute_active
        if not Path(a.active_list).is_file():
            raise SystemExit(f"Active-clients list not found: {a.active_list}")
        log("Working out active clients from the fee list ...")
        active_ids, listed = compute_active(sql_config(a).engine(db), Path(a.active_list), log)
        matched = sum(1 for c in listed if c.households)
        log(f"  {matched} of {len(listed)} listed clients matched; "
            f"{len(active_ids):,} Xplan clients/structures are Active")
    opts = documents.DocOptions(
        dest=Path(a.dest), dry_run=a.dry_run, limit=a.limit, min_free_gb=a.min_free_gb,
        include_notes=not a.no_notes, include_other=not a.no_other,
        online_only=not a.keep_local, entity_table=a.entity_table, entity_key=a.entity_key,
        entity_name=a.entity_name, active_ids=active_ids,
    )
    log(("DRY RUN - nothing will be written. " if a.dry_run else "") + f"Destination: {opts.dest}")
    stats = documents.export_documents(sql_config(a).engine(db), opts, progress=log)
    log("")
    verb = "Would save" if a.dry_run else "Saved"
    log(f"{verb} {stats.files:,} file(s), {stats.bytes_written / 1024 ** 3:,.1f} GB "
        f"({stats.notes:,} file notes); {stats.skipped_existing:,} were already there.")
    if stats.no_client:
        log(f"  {stats.no_client:,} file notes have no client -> Clients\\_No client")
    if stats.unlinked_parts:
        log(f"  {stats.unlinked_parts:,} attached files have no file note -> Clients\\_Unlinked files")
    if stats.folders_moved:
        log(f"  {stats.folders_moved:,} existing client folder(s) moved into Active / Inactive")
    if stats.long_paths:
        log(f"  {stats.long_paths:,} paths are longer than 255 characters")
    if stats.errors:
        log(f"  {len(stats.errors):,} file(s) could not be saved, e.g. {stats.errors[0]}")


def cmd_brightly(a) -> None:
    from .brightly.export import UnsafeDestination, check_destination, export_brightly

    if not a.out:
        raise SystemExit("Set BRIGHTLY_OUT in .env (the secure SharePoint export folder) or pass --out.")
    root = Path(env("BRIGHTLY_OUT")) if env("BRIGHTLY_OUT") else None
    try:
        out = check_destination(Path(a.out), root, a.allow_any_destination)
    except UnsafeDestination as exc:
        raise SystemExit(str(exc))
    db = pick_database(a)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out / (f"sample_{stamp}" if a.sample else f"export_{stamp}")
    log(f"Writing Brightly export to {out}")
    active = Path(a.active_list) if a.active_list else None
    if active and not active.is_file():
        raise SystemExit(f"Active-clients list not found: {active}")
    m = export_brightly(sql_config(a).engine(db), out, a.schema, db, a.sample, progress=log,
                        active_list=active, skip_unmapped=a.skip_unmapped)
    log("")
    for k, v in m["record_counts"].items():
        log(f"  {k}: {v:,}")
    log(f"  unmapped fields: {m['unmapped_fields']:,}  (unmapped.csv)")
    log(f"  TFN values dropped: {m['tfn_fields_dropped']:,}; TFNs removed from text: "
        f"{m['tfn_values_removed_from_text']:,}; sensitive health answers: "
        f"{m['sensitive_health_values']:,}")
    log(f"Done: {out}")


def cmd_profile(a) -> None:
    from . import profile

    db = pick_database(a)
    profile.profile_database(sql_config(a).engine(db), a.out, db, progress=log,
                             include_empty=a.include_empty)


def cmd_describe(a) -> None:
    db = pick_database(a)
    sqlserver.describe_tables(sql_config(a).engine(db), a.patterns, progress=log)


def cmd_tables(a) -> None:
    import csv

    db = pick_database(a)
    rows = sqlserver.table_sizes(sql_config(a).engine(db))
    a.out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", db)
    csv_path = a.out / f"tables_{safe}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["table", "rows", "size_mb", "columns", "binary_columns"])
        for r in rows:
            writer.writerow([f"{r['schema_name']}.{r['table_name']}", r["row_count"],
                             round(float(r["size_mb"]), 1), r["column_count"],
                             r["binary_columns"] or ""])

    total_mb = sum(float(r["size_mb"]) for r in rows)
    with_rows = sum(1 for r in rows if r["row_count"])
    binary_mb = sum(float(r["size_mb"]) for r in rows if r["binary_columns"])
    log(f"{len(rows)} tables ({with_rows} with data, {len(rows) - with_rows} empty), "
        f"{total_mb / 1024:,.1f} GB in total.")
    log(f"Tables with binary columns (stored files/images): {binary_mb / 1024:,.1f} GB")
    log("")
    log(f"{'Table':<55} {'Rows':>13} {'Size MB':>10}  Binary columns")
    for r in rows[: a.top]:
        name = f"{r['schema_name']}.{r['table_name']}"
        log(f"{name[:55]:<55} {r['row_count']:>13,} {float(r['size_mb']):>10,.1f}  "
            f"{(r['binary_columns'] or '')[:40]}")
    if len(rows) > a.top:
        log(f"... {len(rows) - a.top} more")
    log("")
    log(f"Full list: {csv_path}")


def cmd_run(a) -> None:
    bak = a.bak
    if bak is None:
        zip_path = a.zip
        if zip_path is None:
            files = step_download(a)
            zips = [f for f in files if f.suffix.lower() == ".zip"]
            baks = archive.find_backups(files)
            if baks and not zips:
                bak = baks[0]
            elif len(zips) != 1:
                raise SystemExit(
                    "Expected exactly one .zip on the MFT account, found: "
                    + (", ".join(f.name for f in files) or "nothing")
                    + ". Use --remote-file NAME to pick one.")
            else:
                zip_path = zips[0]
        if bak is None:
            a.zip_password = a.zip_password or secret(None, "Zip password")
            extracted = step_unzip(zip_path, a)
            baks = archive.find_backups(extracted)
            if not baks:
                raise SystemExit("No .bak database backup found in the zip. Extracted files: "
                                 + ", ".join(str(p) for p in extracted))
            if len(baks) > 1:
                log("Several backups found; using the first: " + ", ".join(b.name for b in baks))
            bak = baks[0]

    db = step_restore(bak, a)
    step_export(sql_config(a).engine(db), db, a)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xplan_extract",
        description="Download the Iress/Xplan data extract and convert it to Excel and JSON.",
    )
    parser.add_argument("--env-file", default=".env", help="Settings file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    def dirs(p):
        p.add_argument("--download-dir", type=Path, default=Path(env("DOWNLOAD_DIR", "data/download")))
        p.add_argument("--extract-dir", type=Path, default=Path(env("EXTRACT_DIR", "data/extracted")))

    p = sub.add_parser("run", help="Download, unzip, restore and export in one go.")
    add_mft_args(p); add_zip_args(p); add_sql_args(p); add_restore_args(p); add_export_args(p)
    dirs(p)
    p.add_argument("--zip", type=Path, help="Skip the download and use this local zip.")
    p.add_argument("--bak", type=Path, help="Skip download/unzip and restore this .bak.")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("download", help="Download the extract from the Iress MFT account.")
    add_mft_args(p); dirs(p)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("unzip", help="Extract the password-protected zip.")
    p.add_argument("zip", type=Path); add_zip_args(p); dirs(p)
    p.set_defaults(func=cmd_unzip)

    p = sub.add_parser("info", help="Show what is inside a zip and how big it is unzipped.")
    p.add_argument("zip", type=Path)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("restore", help="Restore a .bak file into SQL Server.")
    p.add_argument("bak", type=Path); add_sql_args(p); add_restore_args(p)
    p.add_argument("--check-only", action="store_true",
                   help="Only show how much space the restore needs; don't restore.")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("documents", help="Save the stored documents as files, one folder "
                                         "per client (e.g. into a synced SharePoint folder).")
    p.add_argument("--dest", default=env("DOCUMENTS_DEST"), help="Destination folder.")
    p.add_argument("--dry-run", action="store_true", help="Only count; write nothing.")
    p.add_argument("--limit", type=int, help="Trial run: only the first N file notes.")
    p.add_argument("--shared-examples", type=int, metavar="N",
                   help="Only show N examples of notes linked to several clients.")
    p.add_argument("--min-free-gb", type=float, default=10.0,
                   help="Pause while free disk space is below this (default 10).")
    p.add_argument("--no-notes", action="store_true", help="Don't save file note text.")
    p.add_argument("--no-other", action="store_true", help="Skip _attachmentdata files.")
    p.add_argument("--keep-local", action="store_true",
                   help="Don't mark files online-only for OneDrive.")
    p.add_argument("--active-list", default=env("BRIGHTLY_ACTIVE_LIST"),
                   help="Fees-by-client report (xlsx): client folders go under Clients\\Active "
                        "or Clients\\Inactive (existing folders are moved).")
    p.add_argument("--entity-table", help="Table with client names (auto-detected).")
    p.add_argument("--entity-key", help="Client id column in that table.")
    p.add_argument("--entity-name", help="SQL expression for the client name.")
    p.add_argument("--database", help="SQL Server database (default: the only one restored).")
    add_sql_args(p)
    p.set_defaults(func=cmd_documents)

    p = sub.add_parser("brightly", help="Export households, entities and tasks in Brightly's "
                                        "record shape (JSON Lines).")
    p.add_argument("--out", default=env("BRIGHTLY_OUT"),
                   help="Secure export folder (default BRIGHTLY_OUT from .env).")
    p.add_argument("--schema", default=env("BRIGHTLY_SCHEMA"),
                   help="Brightly factfind_schema.json (default BRIGHTLY_SCHEMA from .env).")
    p.add_argument("--active-list", default=env("BRIGHTLY_ACTIVE_LIST"),
                   help="Fees-by-client report (xlsx): records get \"status\": \"Active\" or \"Inactive\".")
    p.add_argument("--sample", type=int, default=0, metavar="N",
                   help="Only N households (a couple, an SMSF, a trust ...) for checking.")
    p.add_argument("--skip-unmapped", action="store_true",
                   help="Skip the scan for unmapped Xplan fields (faster repeat samples).")
    p.add_argument("--allow-any-destination", action="store_true",
                   help="Allow writing outside BRIGHTLY_OUT (not recommended).")
    p.add_argument("--database", help="SQL Server database (default: the only one restored).")
    add_sql_args(p)
    p.set_defaults(func=cmd_brightly)

    p = sub.add_parser("profile", help="Profile every table/column for data mapping, with "
                                       "personal details masked.")
    p.add_argument("--database", help="SQL Server database (default: the only one restored).")
    p.add_argument("--out", type=Path, default=Path(env("OUTPUT_DIR", "output")))
    p.add_argument("--include-empty", action="store_true")
    add_sql_args(p)
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("describe", help="Show the columns of some tables and what files "
                                        "their binary columns hold (no client data).")
    p.add_argument("patterns", nargs="+", help="Table names, wildcards allowed (e.g. *doc*).")
    p.add_argument("--database", help="SQL Server database (default: the only one restored).")
    add_sql_args(p)
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("tables", help="List the restored tables with row counts and sizes.")
    p.add_argument("--database", help="SQL Server database (default: the only one restored).")
    p.add_argument("--top", type=int, default=40, help="How many of the largest to show.")
    p.add_argument("--out", type=Path, default=Path(env("OUTPUT_DIR", "output")))
    add_sql_args(p)
    p.set_defaults(func=cmd_tables)

    p = sub.add_parser("export", help="Export an already-restored database to Excel/JSON.")
    p.add_argument("--database", help="SQL Server database (default: the only one restored).")
    p.add_argument("--url", help="Any SQLAlchemy URL instead, e.g. sqlite:///file.db")
    add_sql_args(p); add_export_args(p)
    p.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Load .env before building the parser so its values become defaults.
    env_file = ".env"
    for i, arg in enumerate(argv):
        if arg == "--env-file" and i + 1 < len(argv):
            env_file = argv[i + 1]
        elif arg.startswith("--env-file="):
            env_file = arg.split("=", 1)[1]
    load_dotenv(env_file, override=False)

    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (sftp.SftpError, archive.ArchiveError, sqlserver.RestoreError) as exc:
        log(f"\nERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        log("\nCancelled.")
        return 130
    return 0
