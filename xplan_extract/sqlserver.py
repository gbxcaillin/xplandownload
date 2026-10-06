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
