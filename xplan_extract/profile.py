"""Profile the restored database for data mapping, without exposing client data.

For every table with rows and every column: type, how many rows are filled, how many
distinct values, and the *shape* of typical values with letters and digits masked
("Aaaa Aaaaa", "9999-99-99", "a@a.a"). Real values are only shown for short pick-list
columns (few distinct values, e.g. "Email", "Fee Disclosure Statement", "NSW") and never
for columns whose names suggest personal details.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable

import sqlalchemy as sa
import xlsxwriter

Progress = Callable[[str], None]

SAMPLE_ROWS = 2000
STATS_ROWS = 50_000   # bigger tables: fill rates and distinct counts from this many rows
MAX_PICKLIST = 30
SKIP_TYPES = {"varbinary", "binary", "image", "timestamp", "geography", "geometry",
              "hierarchyid", "sql_variant", "xml"}
TEXT_TYPES = {"varchar", "nvarchar", "char", "nchar", "text", "ntext"}
ENTITY_KEYS = ("entity_id", "entityid", "eidobj", "eid", "related_id", "clientid", "client_id")
# Never show real values for columns like these, whatever their cardinality.
PERSONAL = re.compile(
    r"name|surname|given|known_as|email|e_mail|phone|mobile|fax|addr|street|suburb|postcode|"
    r"zip|dob|birth|tfn|tax_?file|abn|acn|medicare|passport|licen[cs]e|account|bsb|iban|"
    r"swift|card|password|secret|note|text|comment|subject|data|content|body|url|ip_?addr",
    re.I,
)


def mask(value: object, limit: int = 24) -> str:
    """Letters -> A/a, digits -> 9, keep punctuation and spaces, collapse long runs."""
    text = str(value).strip()
    if "@" in text and "." in text:
        return "a@a.a"
    shape = re.sub(r"[A-Z]", "A", text)
    shape = re.sub(r"[a-z]", "a", shape)
    shape = re.sub(r"[^\x00-\x7f]", "a", shape)
    shape = re.sub(r"\d", "9", shape)
    shape = re.sub(r"(A|a|9)\1{6,}", lambda m: m.group(1) * 6 + "…", shape)
    return shape[:limit] + ("…" if len(shape) > limit else "")


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def profile_database(engine: sa.Engine, out_dir: Path, label: str,
                     progress: Progress = print, include_empty: bool = False) -> Path:
    from .sqlserver import table_sizes

    out_dir.mkdir(parents=True, exist_ok=True)
    tables = [t for t in table_sizes(engine) if include_empty or t["row_count"]]
    progress(f"Profiling {len(tables)} table(s) ...")
    result = []
    with engine.connect() as conn:
        for i, t in enumerate(sorted(tables, key=lambda t: (t["schema_name"], t["table_name"])), 1):
            full = f"{t['schema_name']}.{t['table_name']}"
            target = f"{_quote(t['schema_name'])}.{_quote(t['table_name'])}"
            rows = int(t["row_count"])
            progress(f"  {i}/{len(tables)} {full} ({rows:,} rows"
                     + (f", sampling {STATS_ROWS:,})" if rows > STATS_ROWS else ")"))
            started = time.monotonic()
            try:
                result.append(_profile_table(conn, full, target, rows))
            except Exception as exc:  # keep going
                result.append({"table": full, "rows": rows,
                               "error": str(exc).splitlines()[0][:300], "columns": []})
            took = time.monotonic() - started
            if took > 20:
                progress(f"     took {took:,.0f}s")

    safe = re.sub(r"[^\w.-]", "_", label)
    json_path = out_dir / f"profile_{safe}.json"
    json_path.write_text(json.dumps({"database": label, "tables": result}, indent=1,
                                    ensure_ascii=False, default=str), encoding="utf-8")
    xlsx_path = out_dir / f"profile_{safe}.xlsx"
    _write_xlsx(xlsx_path, result)
    progress(f"Profile written: {xlsx_path}")
    progress(f"                 {json_path}")
    return xlsx_path


def _profile_table(conn, full: str, target: str, rows: int) -> dict:
    cols = conn.execute(sa.text("""
        SELECT c.name, t.name AS type_name, c.max_length
        FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(:obj) ORDER BY c.column_id
    """), {"obj": target}).all()
    info = {"table": full, "rows": rows, "columns": []}
    sampled = rows > STATS_ROWS
    source = f"(SELECT TOP {STATS_ROWS} * FROM {target}) AS s" if sampled else target
    base_rows = min(rows, STATS_ROWS)
    if sampled:
        info["stats_sampled_rows"] = STATS_ROWS
    names = [c.name for c in cols]
    info["links_to_client_by"] = next((c for c in names if c.lower() in ENTITY_KEYS), None)

    usable = [c for c in cols if c.type_name not in SKIP_TYPES]
    stats: dict[str, tuple[int, int | None]] = {}
    if usable:
        parts = []
        for j, c in enumerate(usable):
            expr = _quote(c.name)
            if c.type_name in ("text", "ntext") or c.max_length == -1:
                expr = f"CAST({expr} AS nvarchar(400))"
            parts.append(f"COUNT({expr}) AS n{j}, COUNT(DISTINCT {expr}) AS d{j}")
        row = conn.execute(sa.text(f"SELECT {', '.join(parts)} FROM {source}")).one()
        for j, c in enumerate(usable):
            stats[c.name] = (row[2 * j], row[2 * j + 1])

        select = ", ".join(
            f"CAST({_quote(c.name)} AS nvarchar(400))" if c.type_name in TEXT_TYPES
            else _quote(c.name) for c in usable)
        sample = conn.execute(sa.text(f"SELECT TOP {SAMPLE_ROWS} {select} FROM {target}")).all()
    else:
        sample = []

    for c in cols:
        entry = {"column": c.name, "type": c.type_name}
        if c.type_name in SKIP_TYPES:
            entry["note"] = "binary/special - not profiled"
            info["columns"].append(entry)
            continue
        filled, distinct = stats.get(c.name, (0, 0))
        entry["filled"] = filled
        entry["filled_pct"] = round(filled * 100 / base_rows, 1) if base_rows else 0
        entry["distinct"] = distinct
        j = [u.name for u in usable].index(c.name)
        values = [r[j] for r in sample if r[j] is not None and str(r[j]).strip() != ""]
        if c.type_name in ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"):
            years = sorted({v.year for v in values if hasattr(v, "year")})
            if years:
                entry["years"] = f"{years[0]}-{years[-1]}"
        else:
            entry["patterns"] = [f"{p} ({n})" for p, n in
                                 Counter(mask(v) for v in values).most_common(3)]
        personal = bool(PERSONAL.search(c.name))
        if (not personal and distinct and distinct <= MAX_PICKLIST and filled >= 20
                and c.type_name in TEXT_TYPES | {"int", "smallint", "tinyint", "bit", "bigint"}):
            top = conn.execute(sa.text(
                f"SELECT TOP {MAX_PICKLIST} LEFT(CAST({_quote(c.name)} AS nvarchar(400)), 50) AS v, "
                f"COUNT(*) AS n FROM {source} WHERE {_quote(c.name)} IS NOT NULL "
                f"GROUP BY LEFT(CAST({_quote(c.name)} AS nvarchar(400)), 50) ORDER BY n DESC")).all()
            entry["values"] = [f"{r.v} ({r.n})" for r in top]
        info["columns"].append(entry)
    return info


def _write_xlsx(path: Path, tables: list[dict]) -> None:
    wb = xlsxwriter.Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False})
    bold = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
    ts = wb.add_worksheet("Tables")
    head = ["Table", "Rows", "Columns", "Links to client by", "Error", "Stats from"]
    for c, h in enumerate(head):
        ts.write(0, c, h, bold)
    for r, t in enumerate(tables, 1):
        ts.write(r, 0, t["table"])
        ts.write(r, 1, t["rows"])
        ts.write(r, 2, len(t["columns"]))
        ts.write(r, 3, t.get("links_to_client_by") or "")
        ts.write(r, 4, t.get("error") or "")
        ts.write(r, 5, f"first {t['stats_sampled_rows']:,} rows" if t.get("stats_sampled_rows")
                 else "all rows")
    ts.set_column(0, 0, 45)
    ts.set_column(3, 4, 22)
    ts.autofilter(0, 0, len(tables), len(head) - 1)
    ts.freeze_panes(1, 0)

    cs = wb.add_worksheet("Columns")
    head = ["Table", "Column", "Type", "Table rows", "Filled", "Filled %", "Distinct",
            "Value shapes (masked)", "Years", "Pick-list values", "Note"]
    for c, h in enumerate(head):
        cs.write(0, c, h, bold)
    r = 0
    for t in tables:
        for col in t["columns"]:
            r += 1
            cs.write(r, 0, t["table"])
            cs.write(r, 1, col["column"])
            cs.write(r, 2, col["type"])
            cs.write(r, 3, t["rows"])
            if "filled" in col:
                cs.write(r, 4, col["filled"])
                cs.write(r, 5, col["filled_pct"])
                cs.write(r, 6, col["distinct"] if col["distinct"] is not None else "")
            cs.write(r, 7, "; ".join(col.get("patterns", [])))
            cs.write(r, 8, col.get("years", ""))
            cs.write(r, 9, "; ".join(col.get("values", [])))
            cs.write(r, 10, col.get("note", ""))
    cs.set_column(0, 1, 32)
    cs.set_column(7, 7, 40)
    cs.set_column(9, 9, 60)
    cs.autofilter(0, 0, max(r, 1), len(head) - 1)
    cs.freeze_panes(1, 2)
    wb.close()
