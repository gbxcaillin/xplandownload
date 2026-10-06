"""Save the documents stored in the Xplan database as normal files, one folder per client.

Xplan keeps them in three tables:

* ``sections_workflow_docnote``  - file notes (email, phone call, review, FDS ...): date, type,
  subject, the note text (``data``) and the clients they belong to (``related_entities`` ->
  ``relation_docnote.id`` -> ``related_id`` = entity id).
* ``sections_workflow_docpart``  - the files attached to a file note (``docid``), one complete
  file per row with its own file name and MIME type.
* ``_attachmentdata``            - other uploaded files (not linked to a client here).

Layout written under the destination folder::

    Clients/<Client name> (<entity id>)/<date> <type> - <subject>.html   (the file note)
    Clients/<Client name> (<entity id>)/<date> <type> - <file name>      (its attachments)
    Clients/_No client/...                                              (notes without a client)
    Other attachments/<field name>/<id> - <file name>
    documents_index.csv                                                 (everything, searchable)

Designed for a OneDrive/SharePoint synced folder: each file is marked "online-only" after it
is written, so OneDrive frees the local copy once uploaded, and the export pauses while free
disk space is low. Re-running skips files that were already written.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import sqlalchemy as sa

Progress = Callable[[str], None]
GB = 1024 ** 3

DOCNOTE = "dbo.sections_workflow_docnote"
DOCPART = "dbo.sections_workflow_docpart"
RELATION = "dbo.relation_docnote"
ATTACHMENTS = "dbo._attachmentdata"

MAX_FOLDER_CHARS = 50
MAX_FILE_CHARS = 90

MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.ms-outlook": ".msg",
    "message/rfc822": ".eml",
    "application/zip": ".zip",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/tiff": ".tif",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
}
SIGNATURE_EXTENSIONS = [
    (b"%PDF", ".pdf"),
    (b"\xd0\xcf\x11\xe0", ".doc"),
    (b"PK\x03\x04", ".zip"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG", ".png"),
    (b"GIF8", ".gif"),
    (b"{\\rtf", ".rtf"),
]

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def safe_name(text: str | None, max_chars: int, fallback: str = "untitled") -> str:
    """A file/folder name that Windows, OneDrive and SharePoint all accept."""
    name = _BAD_CHARS.sub("_", str(text or "")).strip()
    name = re.sub(r"\s+", " ", name)
    while name.startswith(("~$", ".")):
        name = name[2:] if name.startswith("~$") else name[1:]
    name = name.replace("_vti_", "_vti")
    if len(name) > max_chars:
        stem, ext = os.path.splitext(name)
        if 0 < len(ext) <= 8:
            name = stem[: max_chars - len(ext)].rstrip() + ext
        else:
            name = name[:max_chars]
    name = name.rstrip(" .")
    if not name or name.split(".")[0].lower() in _RESERVED:
        name = f"_{name}" if name else fallback
    return name


def extension_for(filename: str | None, mimetype: str | None, head: bytes) -> str:
    ext = os.path.splitext(filename or "")[1]
    if 1 < len(ext) <= 6 and re.fullmatch(r"\.[A-Za-z0-9]+", ext):
        return ""  # already has one
    mt = (mimetype or "").split(";")[0].strip().lower()
    if mt in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mt]
    if mt.startswith("application/vnd.openxmlformats"):
        if "wordprocessing" in mt:
            return ".docx"
        if "spreadsheet" in mt:
            return ".xlsx"
        if "presentation" in mt:
            return ".pptx"
    for sig, sig_ext in SIGNATURE_EXTENSIONS:
        if head.startswith(sig):
            return sig_ext
    return mimetypes.guess_extension(mt) or ""


class FolderNames:
    """Unique (case-insensitive) names per folder, stable across re-runs."""

    def __init__(self):
        self.used: dict[Path, set[str]] = {}

    def take(self, folder: Path, name: str, unique_hint: str) -> str:
        used = self.used.setdefault(folder, set())
        candidate = name
        if candidate.lower() in used:
            stem, ext = os.path.splitext(name)
            suffix = f" ({unique_hint})"
            candidate = stem[: MAX_FILE_CHARS - len(suffix) - len(ext)] + suffix + ext
            n = 2
            while candidate.lower() in used:
                candidate = f"{stem[:MAX_FILE_CHARS - 20]} ({unique_hint}-{n}){ext}"
                n += 1
        used.add(candidate.lower())
        return candidate


# --------------------------------------------------------------------------
# OneDrive / disk space
# --------------------------------------------------------------------------

FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000


def mark_online_only(path: Path) -> bool:
    """Ask OneDrive to free the local copy once uploaded (like "Free up space")."""
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    attrs = kernel32.GetFileAttributesW(str(path))
    if attrs == 0xFFFFFFFF:
        return False
    attrs = (attrs & ~FILE_ATTRIBUTE_PINNED) | FILE_ATTRIBUTE_UNPINNED
    return bool(kernel32.SetFileAttributesW(str(path), attrs))


def wait_for_space(folder: Path, needed: int, min_free: int, progress: Progress,
                   poll_seconds: int = 60, max_wait_hours: float = 24) -> None:
    """Pause until there is room for ``needed`` bytes plus ``min_free`` spare."""
    free = shutil.disk_usage(folder).free
    if free - needed >= min_free:
        return
    progress(f"  Low disk space ({free / GB:,.1f} GB free). Waiting for OneDrive to upload "
             "and free up space... (Ctrl+C to stop; re-run later to continue)")
    deadline = time.monotonic() + max_wait_hours * 3600
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        free = shutil.disk_usage(folder).free
        if free - needed >= min_free + GB:  # some headroom so we don't stop-start
            progress(f"  {free / GB:,.1f} GB free again, continuing.")
            return
        progress(f"  still waiting: {free / GB:,.1f} GB free")
    raise RuntimeError("Disk space did not free up. Check that OneDrive is running and that "
                       "Files On-Demand is turned on, then run the command again.")


# --------------------------------------------------------------------------
# Database lookups
# --------------------------------------------------------------------------

_KEY_COLUMNS = ("entityid", "entity_id", "eid", "eidobj", "id")
_FULL_NAME_COLUMNS = ("display_name", "full_name", "fullname", "client_name",
                      "entity_name", "name")
_FIRST_NAME_COLUMNS = ("preferred_name", "first_name", "firstname", "given_name",
                       "known_as")
_LAST_NAME_COLUMNS = ("last_name", "surname", "lastname", "family_name")


@dataclass
class EntityNames:
    names: dict[int, str] = field(default_factory=dict)
    source: str = "none found - folders will be named by client id"


def find_entity_names(conn, table: str | None = None, key: str | None = None,
                      name_expr: str | None = None) -> EntityNames:
    """Look up client names by entity id, guessing the table/columns unless given."""
    cur = conn.cursor()
    if not table:
        rows = cur.execute("""
            SELECT s.name + '.' + t.name, LOWER(c.name)
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.columns c ON c.object_id = t.object_id
            WHERE t.is_ms_shipped = 0
              AND (t.name = 'entity' OR t.name LIKE 'entity[_]%' OR t.name LIKE 'sections[_]entity%'
                   OR t.name IN ('client', 'clients', 'entities', 'ufield_entity'))
              AND t.name NOT LIKE '%#%'
        """).fetchall()
        tables: dict[str, set[str]] = {}
        for tname, cname in rows:
            tables.setdefault(tname, set()).add(cname)

        def rank(tname: str) -> tuple:
            short = tname.split(".", 1)[1].lower()
            order = ["entity", "ufield_entity", "sections_entity", "client", "clients", "entities"]
            return (order.index(short) if short in order else len(order), len(short))

        for tname in sorted(tables, key=rank):
            cols = tables[tname]
            k = next((c for c in _KEY_COLUMNS if c in cols), None)
            expr = _name_expression(cols)
            if k and expr:
                table, key, name_expr = tname, k, expr
                break
    if not (table and key and name_expr):
        return EntityNames()

    schema, tname = table.split(".", 1) if "." in table else ("dbo", table)
    target = f"[{schema}].[{tname.replace(']', ']]')}]"
    names: dict[int, str] = {}
    for eid, name in cur.execute(f"SELECT [{key}], {name_expr} FROM {target}"):
        try:
            eid = int(eid)
        except (TypeError, ValueError):
            continue
        name = re.sub(r"\s+", " ", str(name or "")).strip()
        if name and eid not in names:
            names[eid] = name
    return EntityNames(names, f"{table} (id: {key}, name: {name_expr}) - {len(names):,} names")


def _name_expression(cols: set[str]) -> str | None:
    full = next((c for c in _FULL_NAME_COLUMNS if c in cols), None)
    first = next((c for c in _FIRST_NAME_COLUMNS if c in cols), None)
    last = next((c for c in _LAST_NAME_COLUMNS if c in cols), None)
    if first and last:
        combined = (f"LTRIM(RTRIM(CONCAT(CAST([{first}] AS nvarchar(200)), ' ', "
                    f"CAST([{last}] AS nvarchar(200)))))")
        return f"COALESCE(NULLIF({combined}, ''), CAST([{full}] AS nvarchar(200)))" if full \
            else combined
    if full:
        return f"CAST([{full}] AS nvarchar(200))"
    if last:
        return f"CAST([{last}] AS nvarchar(200))"
    return None


def _table_exists(conn, name: str) -> bool:
    return conn.cursor().execute("SELECT OBJECT_ID(?, 'U')", name).fetchone()[0] is not None


@dataclass
class Note:
    docid: str
    date: dt.datetime | None
    type: str
    subtype: str
    subject: str
    filename: str
    mimetype: str
    entities: list[int]


def load_notes(conn) -> dict[str, Note]:
    cur = conn.cursor()
    relations: dict[int, list[int]] = {}
    if _table_exists(conn, RELATION):
        for rid, eid in cur.execute(f"SELECT id, related_id FROM {RELATION} "
                                    "WHERE related_id IS NOT NULL ORDER BY id, related_id"):
            relations.setdefault(int(rid), []).append(int(eid))

    notes: dict[str, Note] = {}
    rows = cur.execute(f"""
        SELECT docid, COALESCE([date], created_at), [type], subtype, subject,
               filename, mimetype, related_entities
        FROM {DOCNOTE}
    """).fetchall()
    for docid, date, typ, subtype, subject, filename, mimetype, related in rows:
        key = str(docid).strip()
        notes[key] = Note(key, date, _clean(typ), _clean(subtype), _clean(subject),
                          _clean(filename), _clean(mimetype),
                          relations.get(int(related), []) if related is not None else [])
    return notes


def _clean(value) -> str:
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() == "none" else text


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

@dataclass
class DocOptions:
    dest: Path
    dry_run: bool = False
    limit: int | None = None            # only this many file notes (for a trial run)
    min_free_gb: float = 10.0
    include_notes: bool = True           # save the file note text as .html
    include_other: bool = True           # _attachmentdata
    online_only: bool = True
    entity_table: str | None = None
    entity_key: str | None = None
    entity_name: str | None = None


@dataclass
class Stats:
    notes: int = 0
    files: int = 0
    skipped_existing: int = 0
    bytes_written: int = 0
    no_client: int = 0
    unlinked_parts: int = 0
    long_paths: int = 0
    errors: list[str] = field(default_factory=list)


class _Writer:
    def __init__(self, opts: DocOptions, progress: Progress):
        self.opts = opts
        self.progress = progress
        self.stats = Stats()
        self.names = FolderNames()
        self.min_free = int(opts.min_free_gb * GB)
        self.started = time.monotonic()
        self.last_report = self.started

    def write(self, folder: Path, filename: str, unique_hint: str, data: bytes,
              size: int | None = None) -> Path:
        name = self.names.take(folder, filename, unique_hint)
        path = folder / name
        if len(str(path.absolute())) > 255:
            self.stats.long_paths += 1
        if self.opts.dry_run:
            self.stats.files += 1
            self.stats.bytes_written += len(data) if size is None else size
            return path
        if path.exists() and path.stat().st_size == len(data):
            self.stats.skipped_existing += 1
            return path
        folder.mkdir(parents=True, exist_ok=True)
        wait_for_space(folder, len(data), self.min_free, self.progress)
        tmp = path.with_name(path.name + ".partial")
        with open(_long(tmp), "wb") as fh:
            fh.write(data)
        os.replace(_long(tmp), _long(path))
        if self.opts.online_only:
            mark_online_only(Path(_long(path)))
        self.stats.files += 1
        self.stats.bytes_written += len(data)
        return path

    def report(self, done: int, total: int, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_report < 30:
            return
        self.last_report = now
        rate = self.stats.bytes_written / max(now - self.started, 1)
        self.progress(f"  {done:,}/{total:,} files  {self.stats.bytes_written / GB:,.1f} GB "
                      f"written  {rate / 1024 ** 2:,.1f} MB/s  "
                      f"({self.stats.skipped_existing:,} already there)")


def _long(path: Path) -> str:
    """Windows long-path form so deep OneDrive folders don't hit the 260-char limit."""
    p = str(path.resolve())
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p
    return p


def _note_label(note: Note) -> str:
    date = note.date.strftime("%Y-%m-%d") if isinstance(note.date, dt.datetime) else "no date"
    return f"{date} {note.type}".strip() if note.type else date


def decode_text(data: bytes) -> str:
    """Note text is normally UTF-8; also cope with UTF-16 and old Windows encodings."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if len(data) >= 4 and data[1] == 0 and data[3] == 0:
        return data.decode("utf-16-le", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _note_html(note: Note, client_names: list[str], body: bytes | None,
               attachments: list[str]) -> bytes:
    text = decode_text(body or b"")
    if "html" in note.mimetype.lower() or text.lstrip().startswith("<"):
        content = text
    else:
        content = f"<pre style='white-space:pre-wrap;font-family:inherit'>{html.escape(text)}</pre>"
    links = "; ".join(f"<a href='{html.escape(urllib.parse.quote(a))}'>{html.escape(a)}</a>"
                      for a in attachments)
    rows = [
        ("Date", note.date.strftime("%d/%m/%Y %H:%M") if isinstance(note.date, dt.datetime) else ""),
        ("Type", note.type), ("Sub-type", note.subtype), ("Subject", note.subject),
        ("Client(s)", "; ".join(client_names)),
        ("Xplan document id", note.docid),
    ]
    head = "".join(f"<tr><th align='left'>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
                   for k, v in rows if v)
    if links:
        head += f"<tr><th align='left'>Attachments</th><td>{links}</td></tr>"
    title = html.escape(note.subject or note.type or note.docid)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head>"
            f"<body><table cellpadding='4'>{head}</table><hr>{content}</body></html>"
            ).encode("utf-8")


def export_documents(engine: sa.Engine, opts: DocOptions, progress: Progress = print) -> Stats:
    raw = engine.raw_connection()
    conn = raw.driver_connection if hasattr(raw, "driver_connection") else raw.connection
    try:
        return _export(conn, opts, progress)
    finally:
        raw.close()


def _export(conn, opts: DocOptions, progress: Progress) -> Stats:
    w = _Writer(opts, progress)
    dest = opts.dest
    if not opts.dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    entities = find_entity_names(conn, opts.entity_table, opts.entity_key, opts.entity_name)
    progress(f"Client names from: {entities.source}")
    notes = load_notes(conn)
    linked = sum(1 for n in notes.values() if n.entities)
    progress(f"File notes: {len(notes):,} ({linked:,} linked to a client)")

    def client_folder(eids: list[int]) -> Path:
        if not eids:
            return dest / "Clients" / "_No client"
        eid = eids[0]
        name = entities.names.get(eid)
        label = f"{name} ({eid})" if name else f"Client {eid}"
        return dest / "Clients" / safe_name(label, MAX_FOLDER_CHARS, f"Client {eid}")

    selected = sorted(notes)
    if opts.limit:
        selected = selected[: opts.limit]
        progress(f"Trial run: only the first {len(selected)} file note(s).")
    wanted = set(selected)

    cur = conn.cursor()
    total_parts = cur.execute(f"SELECT COUNT(*) FROM {DOCPART}").fetchone()[0]
    progress(f"Attached files: {total_parts:,}")

    index_rows: list[list] = []
    attachments_by_note: dict[str, list[str]] = {}

    # 1. Attached files, streamed one at a time (some are 100+ MB).
    progress("Saving attached files ...")
    # A dry run only needs each file's first bytes and size, not the 40 GB of content.
    content_sql = ("CAST(SUBSTRING(content, 1, 16) AS varbinary(16))" if opts.dry_run
                   else "content")
    where, params = "", []
    if opts.limit:
        where = f"WHERE docid IN ({', '.join('?' * len(selected))})"
        params = selected
        total_parts = cur.execute(f"SELECT COUNT(*) FROM {DOCPART} {where}", params).fetchone()[0]
    part_cur = conn.cursor()
    part_cur.execute(f"SELECT docid, docpartid, filename, mimetype, created_at, "
                     f"DATALENGTH(content), {content_sql} FROM {DOCPART} {where} "
                     f"ORDER BY docpartid, docid", params)
    done = 0
    for docid, partid, filename, mimetype, created, size, content in part_cur:
        docid = str(docid).strip()
        done += 1
        note = notes.get(docid)
        data = bytes(content or b"")
        size = int(size or 0)
        original = os.path.basename(str(filename or "").replace("\\", "/")) or f"file {partid}"
        name = original + extension_for(original, mimetype, data[:16])
        if note is None:
            w.stats.unlinked_parts += 1
            folder = dest / "Clients" / "_Unlinked files"
            label = created.strftime("%Y-%m-%d") if isinstance(created, dt.datetime) else ""
        else:
            folder = client_folder(note.entities)
            label = _note_label(note)
        file_name = safe_name(f"{label} - {name}" if label else name, MAX_FILE_CHARS, name)
        try:
            path = w.write(folder, file_name, str(partid), data, size)
        except OSError as exc:
            w.stats.errors.append(f"docpart {partid}: {exc}")
            progress(f"  could not save docpart {partid}: {exc}")
            continue
        attachments_by_note.setdefault(docid, []).append(path.name)
        index_rows.append(_index_row("attachment", note, entities, docid, partid, original,
                                     mimetype, size, path, dest))
        w.report(done, total_parts)
    w.report(done, total_parts, force=True)

    # 2. The file notes themselves (note text + details), one small .html each.
    if opts.include_notes:
        progress("Saving file notes ...")
        note_cur = conn.cursor()
        note_cur.execute(f"SELECT docid, data FROM {DOCNOTE} ORDER BY docid")
        for docid, data in note_cur:
            docid = str(docid).strip()
            if docid not in wanted:
                continue
            note = notes[docid]
            folder = client_folder(note.entities)
            if not note.entities:
                w.stats.no_client += 1
            names = [entities.names.get(e, f"Client {e}") for e in note.entities]
            title = note.subject or note.filename or note.subtype or "File note"
            file_name = safe_name(f"{_note_label(note)} - {title}", MAX_FILE_CHARS - 5,
                                  "File note") + ".html"
            body = _note_html(note, names, data, attachments_by_note.get(docid, []))
            try:
                path = w.write(folder, file_name, docid, body)
            except OSError as exc:
                w.stats.errors.append(f"docnote {docid}: {exc}")
                continue
            w.stats.notes += 1
            index_rows.append(_index_row("file note", note, entities, docid, "", title,
                                         note.mimetype, len(body), path, dest))

    # 3. Other attachments (not linked to a client in this extract).
    if opts.include_other and not opts.limit and _table_exists(conn, ATTACHMENTS):
        progress("Saving other attachments ...")
        att_cur = conn.cursor()
        att_cur.execute(f"SELECT id, attachid, fieldname, filename, mimetype, created_at, "
                        f"DATALENGTH(content), {content_sql} FROM {ATTACHMENTS} ORDER BY id")
        for aid, attachid, fieldname, filename, mimetype, created, size, content in att_cur:
            data = bytes(content or b"")
            size = int(size or 0)
            original = os.path.basename(str(filename or "").replace("\\", "/")) or f"file {aid}"
            name = safe_name(f"{aid} - {original}{extension_for(original, mimetype, data[:16])}",
                             MAX_FILE_CHARS, f"file {aid}")
            folder = dest / "Other attachments" / safe_name(fieldname or "general",
                                                            MAX_FOLDER_CHARS, "general")
            try:
                path = w.write(folder, name, str(aid), data, size)
            except OSError as exc:
                w.stats.errors.append(f"_attachmentdata {aid}: {exc}")
                continue
            index_rows.append(["other attachment", "", "", "", "", "", "", "", str(aid),
                               str(attachid or ""), original, mimetype or "", size,
                               str(path.relative_to(dest))])

    if not opts.dry_run:
        index_path = dest / "documents_index.csv"
        with open(_long(index_path), "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["kind", "client_id", "client_name", "other_client_ids", "date",
                             "type", "subtype", "subject", "xplan_docid", "xplan_partid",
                             "original_filename", "mimetype", "bytes", "saved_as"])
            writer.writerows(index_rows)
        progress(f"Index of everything saved: {index_path}")
    return w.stats


def _index_row(kind, note, entities, docid, partid, original, mimetype, size, path, dest):
    eids = note.entities if note else []
    return [
        kind,
        eids[0] if eids else "",
        entities.names.get(eids[0], "") if eids else "",
        ";".join(str(e) for e in eids[1:]),
        note.date.strftime("%Y-%m-%d") if note and isinstance(note.date, dt.datetime) else "",
        note.type if note else "",
        note.subtype if note else "",
        note.subject if note else "",
        docid, partid, original, mimetype or "", size,
        str(path.relative_to(dest)),
    ]
