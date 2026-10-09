"""Links, files and the audit trail, in SQLite (one small file in /data).

A link's token and code are kept encrypted (so staff can show them again) and the token also
as a SHA-256 hash for lookup. File contents never touch the database: only the file's own key,
wrapped by the master key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3
import threading
from pathlib import Path

from . import crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    token_enc BLOB NOT NULL,
    code_enc BLOB NOT NULL,
    client_name TEXT NOT NULL,
    client_ref TEXT,
    client_email TEXT,
    message TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    last_notified_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id),
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    key_enc BLOB NOT NULL,
    scan TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    downloads INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    deleted_by TEXT
);
CREATE INDEX IF NOT EXISTS files_link ON files(link_id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    link_id TEXT,
    file_id TEXT,
    detail TEXT,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id),
    status TEXT NOT NULL,             -- queued | running | done | failed
    file_ids TEXT NOT NULL,           -- JSON list
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    pdf_key_enc BLOB,
    pdf_size INTEGER,
    sharepoint_url TEXT,
    summary TEXT,
    error TEXT,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS reviews_status ON reviews(status, requested_at);
-- the audit trail is append-only
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.isoformat()


def parse(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, data_dir: Path, master_key: bytes):
        self.dir = data_dir
        self.files_dir = data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.key = master_key
        self.lock = threading.RLock()
        self.db = sqlite3.connect(data_dir / "vault.db", check_same_thread=False,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 10000")   # the reviewer shares this file
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "review_id" not in cols:
            self.db.execute("ALTER TABLE files ADD COLUMN review_id TEXT")
        (data_dir / "reviews").mkdir(exist_ok=True)

    # -- helpers -----------------------------------------------------------
    def _one(self, sql: str, *args) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _run(self, sql: str, *args) -> None:
        with self.lock:
            self.db.execute(sql, args)

    def audit(self, actor: str, action: str, link_id: str | None = None,
              file_id: str | None = None, detail: str = "", ip: str = "") -> None:
        self._run("INSERT INTO audit (at, actor, action, link_id, file_id, detail, ip) "
                  "VALUES (?,?,?,?,?,?,?)", iso(now()), actor, action, link_id, file_id,
                  detail[:500], ip[:64])

    # -- links -------------------------------------------------------------
    def create_link(self, client_name: str, created_by: str, days: int,
                    client_ref: str = "", client_email: str = "", message: str = ""
                    ) -> tuple[dict, str, str]:
        """(link, token, code). The token goes in the URL; the code goes to the client by
        another route (SMS or phone)."""
        link_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        created = now()
        self._run(
            "INSERT INTO links (id, token_hash, token_enc, code_enc, client_name, client_ref, "
            "client_email, message, created_by, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            link_id, token_hash(token), crypto.seal(self.key, token.encode(), b"token"),
            crypto.seal(self.key, code.encode(), b"code"), client_name, client_ref or None,
            client_email or None, message or None, created_by, iso(created),
            iso(created + dt.timedelta(days=days)))
        self.audit(created_by, "link created", link_id, detail=f"expires in {days} days")
        return self.link(link_id), token, code

    def link(self, link_id: str) -> dict | None:
        row = self._one("SELECT * FROM links WHERE id = ?", link_id)
        return dict(row) if row else None

    def link_by_token(self, token: str) -> dict | None:
        row = self._one("SELECT * FROM links WHERE token_hash = ?", token_hash(token))
        return dict(row) if row else None

    def secrets_of(self, link: dict) -> tuple[str, str]:
        return (crypto.unseal(self.key, link["token_enc"], b"token").decode(),
                crypto.unseal(self.key, link["code_enc"], b"code").decode())

    @staticmethod
    def state(link: dict) -> str:
        if link["closed_at"]:
            return "closed"
        if link["locked"]:
            return "locked"
        if parse(link["expires_at"]) <= now():
            return "expired"
        return "open"

    def check_code(self, link: dict, code: str, attempts: int) -> bool:
        _, real = self.secrets_of(link)
        ok = hmac.compare_digest(real, (code or "").strip())
        with self.lock:
            if ok:
                self.db.execute("UPDATE links SET failed_attempts = 0 WHERE id = ?",
                                (link["id"],))
            else:
                self.db.execute("UPDATE links SET failed_attempts = failed_attempts + 1, "
                                "locked = CASE WHEN failed_attempts + 1 >= ? THEN 1 ELSE 0 END "
                                "WHERE id = ?", (attempts, link["id"]))
        return ok

    def close_link(self, link_id: str) -> None:
        self._run("UPDATE links SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                  iso(now()), link_id)

    def extend_link(self, link_id: str, days: int) -> None:
        self._run("UPDATE links SET expires_at = ?, closed_at = NULL, locked = 0, "
                  "failed_attempts = 0 WHERE id = ?",
                  iso(now() + dt.timedelta(days=days)), link_id)

    def mark(self, link_id: str, column: str) -> None:
        assert column in ("finished_at", "last_notified_at")
        self._run(f"UPDATE links SET {column} = ? WHERE id = ?", iso(now()), link_id)

    def list_links(self, limit: int = 200) -> list[dict]:
        links = [dict(r) for r in self._all(
            "SELECT * FROM links ORDER BY created_at DESC LIMIT ?", limit)]
        by_link: dict[str, list[dict]] = {}
        if links:
            marks = ",".join("?" * len(links))
            for f in self._all(f"SELECT * FROM files WHERE link_id IN ({marks}) "
                               f"AND deleted_at IS NULL ORDER BY uploaded_at",
                               *[l["id"] for l in links]):
                by_link.setdefault(f["link_id"], []).append(dict(f))
        for l in links:
            l["files"] = by_link.get(l["id"], [])
            l["state"] = self.state(l)
        return links

    # -- files -------------------------------------------------------------
    def file_path(self, link_id: str, file_id: str) -> Path:
        return self.files_dir / link_id / f"{file_id}.enc"

    def count_files(self, link_id: str) -> int:
        return self._one("SELECT COUNT(*) FROM files WHERE link_id = ?", link_id)[0]

    def add_file(self, file_id: str, link_id: str, name: str, size: int, sha256: str,
                 key: bytes, scan: str) -> None:
        self._run("INSERT INTO files (id, link_id, name, size, sha256, key_enc, scan, "
                  "uploaded_at) VALUES (?,?,?,?,?,?,?,?)", file_id, link_id, name, size,
                  sha256, crypto.seal(self.key, key, b"file:" + file_id.encode()), scan,
                  iso(now()))

    def file(self, file_id: str) -> dict | None:
        row = self._one("SELECT * FROM files WHERE id = ?", file_id)
        return dict(row) if row else None

    def file_key(self, f: dict) -> bytes:
        return crypto.unseal(self.key, f["key_enc"], b"file:" + f["id"].encode())

    def counted_download(self, file_id: str) -> None:
        self._run("UPDATE files SET downloads = downloads + 1 WHERE id = ?", file_id)

    def delete_file(self, f: dict, actor: str) -> None:
        path = self.file_path(f["link_id"], f["id"])
        path.unlink(missing_ok=True)
        self._run("UPDATE files SET deleted_at = ?, deleted_by = ? WHERE id = ?",
                  iso(now()), actor, f["id"])

    # -- AI reviews ----------------------------------------------------------
    def enqueue_review(self, link_id: str, requested_by: str) -> dict | None:
        """Queue a review of the link's files not reviewed yet. None when there's nothing new
        (or a review is already waiting)."""
        import json
        with self.lock:
            waiting = self.db.execute("SELECT id FROM reviews WHERE link_id = ? AND status IN "
                                      "('queued', 'running')", (link_id,)).fetchone()
            if waiting:
                return None
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM files WHERE link_id = ? AND deleted_at IS NULL AND "
                "review_id IS NULL ORDER BY uploaded_at", (link_id,))]
            if not ids:
                return None
            review_id = secrets.token_hex(8)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO reviews (id, link_id, status, file_ids, "
                                "requested_by, requested_at) VALUES (?,?,?,?,?,?)",
                                (review_id, link_id, "queued", json.dumps(ids), requested_by,
                                 iso(now())))
                self.db.executemany("UPDATE files SET review_id = ? WHERE id = ?",
                                    [(review_id, i) for i in ids])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        self.audit(requested_by, "review queued", link_id, detail=f"{len(ids)} file(s)")
        return self.review(review_id)

    def review(self, review_id: str) -> dict | None:
        row = self._one("SELECT * FROM reviews WHERE id = ?", review_id)
        return dict(row) if row else None

    def reviews_for(self, link_ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        if link_ids:
            marks = ",".join("?" * len(link_ids))
            for r in self._all(f"SELECT * FROM reviews WHERE link_id IN ({marks}) "
                               f"ORDER BY requested_at", *link_ids):
                out.setdefault(r["link_id"], []).append(dict(r))
        return out

    def claim_review(self) -> dict | None:
        """The oldest queued review, marked running (safe with several workers)."""
        with self.lock:
            row = self.db.execute(
                "UPDATE reviews SET status = 'running', started_at = ?, attempts = attempts + 1 "
                "WHERE id = (SELECT id FROM reviews WHERE status = 'queued' "
                "ORDER BY requested_at LIMIT 1) RETURNING *", (iso(now()),)).fetchone()
        return dict(row) if row else None

    def requeue_stale(self) -> int:
        """Reviews left 'running' by a stopped worker go back in the queue (3 tries)."""
        with self.lock:
            cur = self.db.execute("UPDATE reviews SET status = CASE WHEN attempts >= 3 THEN "
                                  "'failed' ELSE 'queued' END, error = CASE WHEN attempts >= 3 "
                                  "THEN 'stopped part way three times' ELSE error END "
                                  "WHERE status = 'running'")
            return cur.rowcount

    def finish_review(self, review_id: str, status: str, **fields) -> None:
        allowed = {"pdf_key_enc", "pdf_size", "sharepoint_url", "summary", "error", "cost_usd"}
        sets = ", ".join(f"{k} = ?" for k in fields if k in allowed)
        args = [v for k, v in fields.items() if k in allowed]
        self._run(f"UPDATE reviews SET status = ?, finished_at = ?{', ' + sets if sets else ''} "
                  f"WHERE id = ?", status, iso(now()), *args, review_id)

    def review_pdf_path(self, review_id: str) -> Path:
        return self.dir / "reviews" / f"{review_id}.pdf.enc"

    def idle_unreviewed(self, quiet_minutes: int) -> list[str]:
        """Links with new files and no upload for a while (the client didn't press Finished)."""
        cutoff = iso(now() - dt.timedelta(minutes=quiet_minutes))
        return [r[0] for r in self._all(
            "SELECT link_id FROM files WHERE deleted_at IS NULL AND review_id IS NULL "
            "GROUP BY link_id HAVING MAX(uploaded_at) < ?", cutoff)]

    def recent_audit(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT a.*, l.client_name FROM audit a LEFT JOIN links l ON l.id = a.link_id "
            "ORDER BY a.id DESC LIMIT ?", limit)]

    # -- backup ------------------------------------------------------------
    def snapshot(self) -> Path:
        """A consistent copy of the database for the nightly backup."""
        target_dir = self.dir / "snapshot"
        target_dir.mkdir(exist_ok=True)
        tmp = target_dir / "vault.db.tmp"
        tmp.unlink(missing_ok=True)
        with self.lock:
            dest = sqlite3.connect(tmp)
            self.db.backup(dest)
            dest.close()
        tmp.replace(target_dir / "vault.db")
        return target_dir / "vault.db"
