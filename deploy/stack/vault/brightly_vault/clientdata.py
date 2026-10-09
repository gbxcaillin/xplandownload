"""The client's current record from the Brightly database, to compare documents against.
Optional: without BRIGHTLY_DB_URL (or before the data is loaded) the review still lists what
the documents say, just without "currently on file" values."""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import re

from .privacy import without_health

log = logging.getLogger("vault.reviewer")

CHILD_TABLES = {
    "contacts": ("contact_point", "kind, value, is_primary"),
    "accounts": ("account", "*"),
    "assets_liabilities": ("asset_liability", "*"),
    "goals": ("goal", "*"),
}
SKIP = {"id", "family_group_id", "import_run_id", "imported_at", "updated_at", "source",
        "raw", "xplan_row"}


def _plain(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _rows(cur, sql, args) -> list[dict]:
    cur.execute(sql, args)
    cols = [c.name for c in cur.description]
    return [{k: _plain(v) for k, v in zip(cols, row) if k not in SKIP and v not in (None, "")}
            for row in cur.fetchall()]


def family_group_key(client_ref: str | None) -> tuple[str, str] | None:
    """("xplan_id", "12345") from "H-12345" or "12345"; ("name", ...) is never guessed."""
    ref = (client_ref or "").strip()
    m = re.fullmatch(r"(?:H-)?(\d{1,12})", ref, re.I)
    return ("xplan_id", m.group(1)) if m else None


def load_record(db_url: str | None, client_ref: str | None) -> dict | None:
    key = family_group_key(client_ref)
    if not db_url or not key:
        return None
    try:
        import psycopg
    except ImportError:
        log.warning("psycopg not installed: reviewing without the client record")
        return None
    try:
        with psycopg.connect(db_url, connect_timeout=10) as conn, conn.cursor() as cur:
            groups = _rows(cur, "SELECT *, id AS fg_pk FROM family_group WHERE xplan_id = %s "
                                "OR id = %s", (key[1], f"H-{key[1]}"))
            if not groups:
                return None
            fg = groups[0]
            pk = fg.pop("fg_pk")
            fg.pop("ff_counts", None)
            record = {"family_group": fg,
                      "people": _rows(cur, "SELECT * FROM person WHERE family_group_id = %s "
                                           "ORDER BY position", (pk,))}
            for name, (table, cols) in CHILD_TABLES.items():
                record[name] = _rows(cur, f"SELECT {cols} FROM {table} WHERE "
                                          f"family_group_id = %s", (pk,))
            return without_health(record)
    except Exception as exc:   # the review still runs, without current values
        log.warning("couldn't read the client record: %s", exc)
        return None
