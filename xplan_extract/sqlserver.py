"""Connect to SQL Server and restore the .bak database backup."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import shutil
import struct
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath
from typing import Callable

import sqlalchemy as sa

Progress = Callable[[str], None]
GB = 1024 ** 3


class RestoreError(Exception):
    pass


@dataclass
class SqlServerConfig:
    server: str = "localhost,1433"
    user: str | None = None
    password: str | None = None
    trusted_connection: bool = False   # Windows authentication
    driver: str | None = None          # auto-detected when empty
    trust_server_certificate: bool = True

    def odbc_string(self, database: str = "master") -> str:
        driver = self.driver or detect_odbc_driver()
        parts = [f"DRIVER={{{driver}}}", f"SERVER={self.server}", f"DATABASE={_odbc_escape(database)}"]
        if self.trusted_connection or not self.user:
            parts.append("Trusted_Connection=yes")
        else:
            parts += [f"UID={_odbc_escape(self.user)}", f"PWD={_odbc_escape(self.password or '')}"]
        if self.trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"

    def engine(self, database: str) -> sa.Engine:
        url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(self.odbc_string(database))
        engine = sa.create_engine(url, pool_pre_ping=True)
        sa.event.listen(engine, "connect", _install_output_converters)
        return engine


def _odbc_escape(value: str) -> str:
    if re.search(r"[;{}=\s]", value) or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def detect_odbc_driver() -> str:
    import pyodbc

    drivers = pyodbc.drivers()
    versioned = sorted(
        (d for d in drivers if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
        key=lambda d: int(re.search(r"\d+", d).group()),
    )
    if versioned:
        return versioned[-1]
    for name in ("SQL Server Native Client 11.0", "SQL Server"):
        if name in drivers:
            return name
    raise RestoreError(
        "No SQL Server ODBC driver found. Install 'Microsoft ODBC Driver 18 for SQL Server' "
        "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)."
    )


def _datetimeoffset(raw: bytes) -> datetime:
    # SQL_SS_TIMESTAMPOFFSET_STRUCT
    y, mo, d, h, mi, s, ns, oh, om = struct.unpack("<6hI2h", raw)
    return datetime(y, mo, d, h, mi, s, ns // 1000,
                    tzinfo=timezone(timedelta(hours=oh, minutes=om)))


def _install_output_converters(dbapi_conn, _record) -> None:
    if hasattr(dbapi_conn, "add_output_converter"):
        dbapi_conn.add_output_converter(-155, _datetimeoffset)


def user_databases(cfg: SqlServerConfig) -> list[str]:
    conn = _connect_master(cfg)
    try:
        rows = conn.cursor().execute(
            "SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' "
            "AND name NOT IN ('ReportServer', 'ReportServerTempDB') ORDER BY name").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def table_sizes(engine: sa.Engine) -> list[dict]:
    """Every user table with its row count, size on disk and binary columns."""
    sql = sa.text("""
        SELECT s.name AS schema_name, t.name AS table_name,
               SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END) AS row_count,
               SUM(ps.reserved_page_count) * 8 / 1024.0 AS size_mb,
               (SELECT COUNT(*) FROM sys.columns c
                 WHERE c.object_id = t.object_id) AS column_count,
               STUFF((SELECT ', ' + c.name FROM sys.columns c
                        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                       WHERE c.object_id = t.object_id
                         AND ty.name IN ('varbinary', 'binary', 'image')
                       FOR XML PATH('')), 1, 2, '') AS binary_columns
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
        WHERE t.is_ms_shipped = 0
        GROUP BY s.name, t.name, t.object_id
        ORDER BY size_mb DESC, s.name, t.name
    """)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(sql)]


FILE_SIGNATURES = [
    (b"%PDF", "PDF"),
    (b"PK\x03\x04", "ZIP / Office 2007+ (docx, xlsx, pptx)"),
    (b"\xd0\xcf\x11\xe0", "Office 97-2003 (doc, xls, msg)"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG", "PNG"),
    (b"GIF8", "GIF"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"{\\rtf", "RTF"),
    (b"\x1f\x8b", "GZIP-compressed"),
    (b"\x78\x9c", "zlib-compressed"),
    (b"\x78\xda", "zlib-compressed"),
    (b"\x78\x01", "zlib-compressed"),
    (b"BZh", "BZIP2-compressed"),
    (b"\xef\xbb\xbf", "UTF-8 text"),
    (b"\xff\xfe", "UTF-16 text"),
    (b"<", "HTML / XML"),
]

_DESCRIBE_VALUE_HINTS = ("ext", "mime", "type", "format", "kind")


def file_kind(head: bytes | None) -> str:
    if not head:
        return "(empty)"
    for sig, name in FILE_SIGNATURES:
        if head.startswith(sig):
            return name
    if all(32 <= b < 127 or b in (9, 10, 13) for b in head):
        return "plain text"
    return "unknown (starts " + head[:4].hex(" ").upper() + ")"


def describe_tables(engine: sa.Engine, patterns: list[str], progress: Progress = print,
                    sample_rows: int = 300) -> None:
    """Column layout of matching tables, plus what kind of files binary columns hold.

    Prints no row values except short values of type/extension-like columns.
    """
    import fnmatch
    from collections import Counter

    sizes = {f"{r['schema_name']}.{r['table_name']}": r for r in table_sizes(engine)}
    names = [n for n in sizes if any(
        fnmatch.fnmatchcase(n.lower(), p.lower()) or
        fnmatch.fnmatchcase(n.split(".", 1)[1].lower(), p.lower()) for p in patterns)]
    if not names:
        progress("No tables match " + ", ".join(patterns))
        return
    prep = engine.dialect.identifier_preparer
    with engine.connect() as conn:
        for full in sorted(names):
            info = sizes[full]
            schema, table = full.split(".", 1)
            target = f"{prep.quote_schema(schema)}.{prep.quote(table)}"
            progress("")
            progress(f"== {full}: {info['row_count']:,} rows, {float(info['size_mb']):,.1f} MB")
            cols = conn.execute(sa.text("""
                SELECT c.name, t.name AS type_name, c.max_length, c.is_nullable,
                       CASE WHEN EXISTS (SELECT 1 FROM sys.index_columns ic
                             JOIN sys.indexes i ON i.object_id = ic.object_id
                                               AND i.index_id = ic.index_id
                             WHERE i.is_primary_key = 1 AND ic.object_id = c.object_id
                               AND ic.column_id = c.column_id) THEN 1 ELSE 0 END AS is_pk
                FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id
                WHERE c.object_id = OBJECT_ID(:obj) ORDER BY c.column_id
            """), {"obj": target}).all()
            for c in cols:
                length = "" if c.max_length in (None, 0) else (
                    "(max)" if c.max_length == -1 else f"({c.max_length})")
                flags = " PK" if c.is_pk else ""
                progress(f"   {c.name:<40} {c.type_name}{length}{flags}")

            for c in cols:
                col = prep.quote(c.name)
                if c.type_name in ("varbinary", "binary", "image"):
                    rows = conn.execute(sa.text(
                        f"SELECT TOP {int(sample_rows)} CAST(SUBSTRING({col}, 1, 16) AS varbinary(16)), "
                        f"DATALENGTH({col}) FROM {target} WHERE {col} IS NOT NULL")).all()
                    kinds = Counter(file_kind(bytes(r[0]) if r[0] is not None else None)
                                    for r in rows)
                    stats = conn.execute(sa.text(
                        f"SELECT COUNT(*), AVG(CAST(DATALENGTH({col}) AS float)), "
                        f"MAX(DATALENGTH({col})) FROM {target} WHERE {col} IS NOT NULL")).one()
                    progress(f"   -> {c.name}: {stats[0]:,} non-empty, average "
                             f"{(stats[1] or 0) / 1024:,.0f} KB, largest "
                             f"{(stats[2] or 0) / 1024 ** 2:,.1f} MB")
                    for kind, n in kinds.most_common(8):
                        progress(f"        {kind}: {n} of first {len(rows)}")
                elif (any(h in c.name.lower() for h in _DESCRIBE_VALUE_HINTS)
                      and c.type_name in ("varchar", "nvarchar", "char", "nchar", "int",
                                          "smallint", "tinyint")):
                    rows = conn.execute(sa.text(
                        f"SELECT TOP 10 LEFT(CAST({col} AS nvarchar(100)), 30) AS v, COUNT(*) AS n "
                        f"FROM {target} GROUP BY LEFT(CAST({col} AS nvarchar(100)), 30) "
                        f"ORDER BY n DESC")).all()
                    shown = ", ".join(f"{r.v!s}: {r.n:,}" for r in rows)
                    progress(f"   -> {c.name} most common values: {shown}")


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------

def _sql_str(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def _sql_name(value: str) -> str:
    return "[" + value.replace("]", "]]") + "]"


def _connect_master(cfg: SqlServerConfig):
    import pyodbc

    try:
        return pyodbc.connect(cfg.odbc_string("master"), autocommit=True, timeout=30)
    except pyodbc.Error as exc:
        raise RestoreError(f"Could not connect to SQL Server '{cfg.server}': {exc}") from exc


def _drain(cursor) -> None:
    """RESTORE emits progress messages as result sets; consume them all or it aborts."""
    while cursor.nextset():
        pass


def server_path_for(local_bak: Path, share_local: Path | None, share_server: str | None,
                    progress: Progress = print) -> str:
    """Return the path SQL Server should use to read the backup.

    When SQL Server runs elsewhere (e.g. Docker), ``share_local`` is a folder the server
    sees as ``share_server``; the backup is copied there if it isn't already inside it.
    """
    if not share_server:
        return str(local_bak.resolve())
    if share_local is None:
        raise RestoreError("--backup-share-local is required with --backup-share-server")
    share_local = share_local.resolve()
    local_bak = local_bak.resolve()
    if share_local not in local_bak.parents:
        share_local.mkdir(parents=True, exist_ok=True)
        dest = share_local / local_bak.name
        if not (dest.exists() and dest.stat().st_size == local_bak.stat().st_size):
            progress(f"Copying backup into SQL Server share: {dest}")
            shutil.copy2(local_bak, dest)
        local_bak = dest
    rel = local_bak.relative_to(share_local).parts
    joiner = ntpath if "\\" in share_server else posixpath
    return joiner.join(share_server, *rel)


def _check_space(files: list[dict], data_dir: str, log_dir: str, progress: Progress,
                 enforce: bool) -> None:
    """Report how big the restored database will be and stop if it won't fit."""
    gb = 1024 ** 3
    needs: dict[str, int] = {}
    for f in files:
        folder = log_dir if f["Type"] == "L" else data_dir
        size = int(f.get("Size") or 0)
        progress(f"  {f['LogicalName']} ({'log' if f['Type'] == 'L' else 'data'}): "
                 f"{size / gb:,.1f} GB -> {folder}")
        needs[folder] = needs.get(folder, 0) + size
    progress(f"Restored database needs {sum(needs.values()) / gb:,.1f} GB in total.")

    by_drive: dict[int, list] = {}
    for folder, size in needs.items():
        if not os.path.isdir(folder):
            continue  # SQL Server is on another machine/container; it checks for itself
        try:
            usage = shutil.disk_usage(folder)
        except OSError:
            continue
        key = os.stat(folder).st_dev  # same device = same pool of free space
        entry = by_drive.setdefault(key, [0, usage.free, folder])
        entry[0] += size
    for need, free, folder in by_drive.values():
        progress(f"Free space where SQL Server will put it ({folder}): {free / gb:,.1f} GB")
        if need + 2 * gb > free and enforce:
            raise RestoreError(
                f"Not enough disk space: the restored database needs {need / gb:,.1f} GB "
                f"(+2 GB spare) but only {free / gb:,.1f} GB is free in {folder}. "
                "Free up space, or restore onto another (NTFS) drive with "
                "--data-dir D:\\SQLData --log-dir D:\\SQLData."
            )


def restore_backup(
    cfg: SqlServerConfig,
    bak_server_path: str,
    database_name: str | None = None,
    replace: bool = False,
    data_dir: str | None = None,
    log_dir: str | None = None,
    progress: Progress = print,
    check_only: bool = False,
) -> str:
    """Restore the backup and return the database name."""
    import pyodbc

    conn = _connect_master(cfg)
    cur = conn.cursor()
    disk = _sql_str(bak_server_path)
    try:
        cur.execute(f"RESTORE HEADERONLY FROM DISK = {disk}")
        cols = [c[0] for c in cur.description]
        headers = [dict(zip(cols, r)) for r in cur.fetchall()]
        _drain(cur)
    except pyodbc.Error as exc:
        raise RestoreError(
            f"SQL Server could not read the backup at {bak_server_path}: {exc}\n"
            "Make sure the path is visible to the SQL Server service (see README) and that "
            "your SQL Server version is the same or newer than the one that made the backup."
        ) from exc

    full = [h for h in headers if h.get("BackupType") == 1] or headers
    if not full:
        raise RestoreError("The backup file contains no backup sets.")
    header = full[-1]
    position = header.get("Position", 1)
    db = database_name or header["DatabaseName"]
    progress(f"Backup contains database '{header['DatabaseName']}' "
             f"(made {header.get('BackupFinishDate')}, SQL Server {header.get('SoftwareVersionMajor')}."
             f"{header.get('SoftwareVersionMinor')}); restoring as '{db}'.")

    cur.execute("SELECT DB_ID(?)", db)
    exists = cur.fetchone()[0] is not None
    if exists and not replace and not check_only:
        raise RestoreError(
            f"Database '{db}' already exists. Use --replace to overwrite it, "
            "--database-name to restore under another name, or skip the restore and run "
            f"'export --database {db}'."
        )

    cur.execute(f"RESTORE FILELISTONLY FROM DISK = {disk} WITH FILE = {int(position)}")
    cols = [c[0] for c in cur.description]
    files = [dict(zip(cols, r)) for r in cur.fetchall()]
    _drain(cur)

    cur.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(4000)), "
                "CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(4000))")
    default_data, default_log = cur.fetchone()
    data_dir = data_dir or default_data
    log_dir = log_dir or default_log or data_dir
    if not data_dir:
        raise RestoreError("Could not determine SQL Server's data folder; pass --data-dir.")

    moves = []
    for f in files:
        logical = f["LogicalName"]
        original = f["PhysicalName"] or logical
        orig_name = ntpath.basename(original) if "\\" in original else posixpath.basename(original)
        ext = PurePath(orig_name).suffix if f["Type"] in ("D", "L") else ""
        folder = log_dir if f["Type"] == "L" else data_dir
        joiner = ntpath if "\\" in folder else posixpath
        safe = re.sub(r"[^\w.-]", "_", f"{db}_{logical}")
        moves.append(f"MOVE {_sql_str(logical)} TO {_sql_str(joiner.join(folder, safe + ext))}")

    _check_space(files, data_dir, log_dir, progress, enforce=not check_only)
    if check_only:
        conn.close()
        return db

    options = [f"FILE = {int(position)}", *moves, "RECOVERY", "STATS = 10"]
    if replace:
        options.append("REPLACE")
    sql = f"RESTORE DATABASE {_sql_name(db)} FROM DISK = {disk} WITH {', '.join(options)}"
    progress(f"Restoring database '{db}' (this can take a while for large backups) ...")
    try:
        if exists:
            cur.execute(f"ALTER DATABASE {_sql_name(db)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cur.execute(sql)
        _drain(cur)
    except pyodbc.Error as exc:
        raise RestoreError(f"Restore failed: {exc}") from exc
    finally:
        conn.close()
    progress(f"Database '{db}' restored.")
    return db


# --------------------------------------------------------------------------
# Backup (to re-create the raw .bak from the restored database)
# --------------------------------------------------------------------------

def backup_database(cfg: SqlServerConfig, db: str, dest_dir: str | None = None,
                    progress: Progress = print) -> str:
    """Full COPY_ONLY backup with checksums, then RESTORE VERIFYONLY. Named '<db>.bak' so the
    archive keeps the extract's snapshot date. Returns the backup's path (as the server sees it)."""
    import pyodbc

    conn = _connect_master(cfg)
    cur = conn.cursor()
    try:
        cur.execute("SELECT DB_ID(?)", db)
        if cur.fetchone()[0] is None:
            raise RestoreError(f"Database '{db}' doesn't exist on this server.")
        if not dest_dir:
            cur.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(4000))")
            dest_dir = cur.fetchone()[0]
        if not dest_dir:
            raise RestoreError("Could not find SQL Server's backup folder; pass --dest.")
        joiner = ntpath if "\\" in dest_dir else posixpath
        path = joiner.join(dest_dir, re.sub(r"[^\w.-]", "_", db) + ".bak")

        # allocated size of the data files (FILEPROPERTY would read master's, not db's, files)
        cur.execute("SELECT SUM(CAST(size AS bigint)) * 8192 FROM sys.master_files "
                    "WHERE database_id = DB_ID(?) AND type = 0", db)
        used = int(cur.fetchone()[0] or 0)
        if Path(dest_dir).exists():
            free = shutil.disk_usage(dest_dir).free
            progress(f"Database data files: {used / GB:,.1f} GB; {free / GB:,.1f} GB free in {dest_dir}.")
            if used + 2 * GB > free:
                raise RestoreError(f"Not enough space in {dest_dir} for the backup (up to "
                                   f"{used / GB:,.0f} GB). Pass --dest on a drive with more room.")

        progress(f"Backing up '{db}' to {path} (compressed, with checksums) - this can take "
                 "15-30 minutes ...")
        base = f"BACKUP DATABASE {_sql_name(db)} TO DISK = {_sql_str(path)} WITH COPY_ONLY, " \
               f"CHECKSUM, INIT, STATS = 10"
        try:
            cur.execute(base + ", COMPRESSION")
            _drain(cur)
        except pyodbc.Error as exc:
            if "compress" not in str(exc).lower():
                raise RestoreError(f"Backup failed: {exc}") from exc
            progress("This SQL Server edition can't compress backups; backing up uncompressed.")
            cur.execute(base)
            _drain(cur)

        progress("Verifying the backup (RESTORE VERIFYONLY) ...")
        try:
            cur.execute(f"RESTORE VERIFYONLY FROM DISK = {_sql_str(path)} WITH CHECKSUM")
            _drain(cur)
        except pyodbc.Error as exc:
            raise RestoreError(f"The backup didn't verify: {exc}") from exc
    finally:
        conn.close()
    progress(f"Backup made and verified: {path}")
    return path
