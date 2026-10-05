"""Export every table in a database to Excel (.xlsx) and JSON.

Works with any SQLAlchemy engine. SQL Server gets special handling for column
types the ODBC driver cannot return directly (geography, hierarchyid, ...).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import re
import uuid
import warnings
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

import sqlalchemy as sa
import xlsxwriter

EXCEL_MAX_ROWS = 1_048_576          # including the header row
EXCEL_MAX_CELL_CHARS = 32_767
EXCEL_MAX_SHEET_NAME = 31
EXCEL_MAX_SAFE_INT = 2 ** 53        # Excel stores numbers as doubles
TRUNCATION_MARK = "…[truncated]"

# SQL Server system schemas/objects that are never client data.
MSSQL_SKIP_TABLES = {("dbo", "sysdiagrams")}

# SQL Server types the ODBC driver can't return as-is: select an expression instead.
MSSQL_TYPE_EXPRESSIONS = {
    "geography": "{col}.STAsText()",
    "geometry": "{col}.STAsText()",
    "hierarchyid": "{col}.ToString()",
    "sql_variant": "CAST({col} AS nvarchar(4000))",
}

Progress = Callable[[str], None]


@dataclass
class Column:
    name: str
    type: str
    nullable: bool | None = None


@dataclass
class TableInfo:
    schema: str | None
    name: str
    kind: str = "table"  # "table" or "view"
    columns: list[Column] = field(default_factory=list)
    row_count: int = 0
    json_file: str | None = None
    excel_file: str | None = None
    excel_sheets: list[str] = field(default_factory=list)
    truncated_cells: int = 0
    error: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


# --------------------------------------------------------------------------
# Value conversion
# --------------------------------------------------------------------------

def _decimal_to_number(value: Decimal) -> int | float | str:
    """Return an int/float when that is lossless, otherwise the exact string."""
    if not value.is_finite():
        return str(value)
    if value == value.to_integral_value():
        as_int = int(value)
        if abs(as_int) < EXCEL_MAX_SAFE_INT:
            return as_int
        return str(value)
    as_float = float(value)
    if Decimal(repr(as_float)) == value.normalize():
        return as_float
    return str(value)


def to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return _decimal_to_number(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return str(value)


# --------------------------------------------------------------------------
# Naming helpers
# --------------------------------------------------------------------------

_SHEET_BAD_CHARS = re.compile(r"[\[\]:*?/\\]")
_FILE_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class UniqueNames:
    """Hands out names that are unique case-insensitively (Excel and Windows)."""

    def __init__(self, max_len: int | None = None):
        self.max_len = max_len
        self.used: set[str] = set()

    def take(self, wanted: str) -> str:
        name = wanted[: self.max_len] if self.max_len else wanted
        n = 2
        while name.lower() in self.used:
            suffix = f"~{n}"
            base = wanted[: self.max_len - len(suffix)] if self.max_len else wanted
            name = base + suffix
            n += 1
        self.used.add(name.lower())
        return name


def sheet_name_for(table: TableInfo) -> str:
    base = table.name if table.schema in (None, "dbo", "main") else table.full_name
    base = _SHEET_BAD_CHARS.sub("_", base).strip("'") or "table"
    return base


def file_stem_for(table: TableInfo) -> str:
    stem = _FILE_BAD_CHARS.sub("_", table.full_name).strip(" .") or "table"
    return stem[:150]


# --------------------------------------------------------------------------
# Table discovery
# --------------------------------------------------------------------------

def list_tables(
    engine: sa.Engine,
    schemas: Iterable[str] | None = None,
    include_views: bool = False,
    only: Iterable[str] | None = None,
) -> list[TableInfo]:
    schemas = list(schemas) if schemas else None
    if engine.dialect.name == "mssql":
        tables = _list_tables_mssql(engine, include_views)
    else:
        tables = _list_tables_generic(engine, schemas, include_views)

    if schemas:
        wanted = {s.lower() for s in schemas}
        tables = [t for t in tables if (t.schema or "").lower() in wanted]
    if only:
        wanted = {o.lower() for o in only}
        tables = [t for t in tables if t.full_name.lower() in wanted or t.name.lower() in wanted]
    return tables


def _list_tables_mssql(engine: sa.Engine, include_views: bool) -> list[TableInfo]:
    types = "('U', 'V')" if include_views else "('U')"
    sql = sa.text(f"""
        SELECT s.name AS schema_name, o.name AS object_name, o.type AS object_type,
               c.name AS column_name, t.name AS type_name, c.max_length,
               c.precision, c.scale, c.is_nullable
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.columns c ON c.object_id = o.object_id
        JOIN sys.types t ON t.user_type_id = c.user_type_id
        WHERE o.is_ms_shipped = 0 AND o.type IN {types}
        ORDER BY s.name, o.name, c.column_id
    """)
    tables: dict[tuple[str, str], TableInfo] = {}
    with engine.connect() as conn:
        for row in conn.execute(sql):
            key = (row.schema_name, row.object_name)
            if key in MSSQL_SKIP_TABLES:
                continue
            if key not in tables:
                kind = "view" if row.object_type.strip() == "V" else "table"
                tables[key] = TableInfo(row.schema_name, row.object_name, kind)
            tables[key].columns.append(
                Column(row.column_name, _mssql_type_label(row), bool(row.is_nullable))
            )
    return list(tables.values())


def _mssql_type_label(row: Any) -> str:
    name = row.type_name
    if name in ("varchar", "char", "varbinary", "binary"):
        return f"{name}({'max' if row.max_length == -1 else row.max_length})"
    if name in ("nvarchar", "nchar"):
        return f"{name}({'max' if row.max_length == -1 else row.max_length // 2})"
    if name in ("decimal", "numeric"):
        return f"{name}({row.precision},{row.scale})"
    return name


def _list_tables_generic(
    engine: sa.Engine, schemas: list[str] | None, include_views: bool
) -> list[TableInfo]:
    inspector = sa.inspect(engine)
    found: list[TableInfo] = []
    for schema in schemas or [None]:
        names = [(n, "table") for n in inspector.get_table_names(schema=schema)]
        if include_views:
            names += [(n, "view") for n in inspector.get_view_names(schema=schema)]
        for name, kind in sorted(names):
            info = TableInfo(schema, name, kind)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    for col in inspector.get_columns(name, schema=schema):
                        try:
                            type_label = str(col["type"])
                        except Exception:
                            type_label = type(col["type"]).__name__
                        info.columns.append(Column(col["name"], type_label, col.get("nullable")))
                except Exception:
                    pass  # columns are then taken from the result set
            found.append(info)
    return found


def _select_sql(engine: sa.Engine, table: TableInfo) -> str:
    prep = engine.dialect.identifier_preparer
    target = prep.quote(table.name)
    if table.schema:
        target = f"{prep.quote_schema(table.schema)}.{target}"

    if engine.dialect.name == "mssql" and table.columns:
        parts = []
        for col in table.columns:
            quoted = prep.quote(col.name)
            base_type = col.type.split("(")[0]
            expr = MSSQL_TYPE_EXPRESSIONS.get(base_type)
            parts.append(f"{expr.format(col=quoted)} AS {quoted}" if expr else quoted)
        return f"SELECT {', '.join(parts)} FROM {target}"
    return f"SELECT * FROM {target}"


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

class JsonTableWriter:
    """Streams rows of one table to a JSON array (or JSON Lines) file."""

    def __init__(self, path: Path, jsonl: bool = False):
        self.path = path
        self.jsonl = jsonl
        self.fh = open(path, "w", encoding="utf-8", newline="\n")
        self.count = 0
        if not jsonl:
            self.fh.write("[")

    def write(self, row: dict) -> None:
        text = json.dumps(row, ensure_ascii=False, default=str)
        if self.jsonl:
            self.fh.write(text + "\n")
        else:
            self.fh.write(("\n  " if self.count == 0 else ",\n  ") + text)
        self.count += 1

    def close(self) -> None:
        if not self.jsonl:
            self.fh.write("\n]\n" if self.count else "]\n")
        self.fh.close()


class CombinedJsonWriter:
    """Streams all tables into one JSON object: {"tables": {"schema.table": [...]}}."""

    def __init__(self, path: Path, database: str | None):
        self.path = path
        self.fh = open(path, "w", encoding="utf-8", newline="\n")
        self.fh.write("{\n")
        self.fh.write(f'"database": {json.dumps(database)},\n')
        self.fh.write('"tables": {')
        self.tables = 0
        self.rows = 0

    def begin_table(self, name: str) -> None:
        self.fh.write(("\n" if self.tables == 0 else ",\n") + json.dumps(name) + ": [")
        self.tables += 1
        self.rows = 0

    def write(self, row: dict) -> None:
        text = json.dumps(row, ensure_ascii=False, default=str)
        self.fh.write(("\n  " if self.rows == 0 else ",\n  ") + text)
        self.rows += 1

    def end_table(self) -> None:
        self.fh.write("\n]" if self.rows else "]")

    def close(self) -> None:
        self.fh.write("\n}\n}\n")
        self.fh.close()


class ExcelWorkbook:
    """A write-only xlsx workbook that holds one or more tables."""

    def __init__(self, path: Path, with_index: bool, max_rows_per_sheet: int):
        self.path = path
        self.max_data_rows = max_rows_per_sheet
        self.wb = xlsxwriter.Workbook(
            str(path),
            {
                "constant_memory": True,
                "strings_to_formulas": False,
                "strings_to_urls": False,
                "strings_to_numbers": False,
                "use_zip64": True,
                "default_date_format": "yyyy-mm-dd hh:mm:ss",
            },
        )
        self.names = UniqueNames(EXCEL_MAX_SHEET_NAME)
        self.fmt_header = self.wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
        self.fmt_datetime = self.wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        self.fmt_date = self.wb.add_format({"num_format": "yyyy-mm-dd"})
        self.fmt_time = self.wb.add_format({"num_format": "hh:mm:ss"})
        self.fmt_link = self.wb.add_format({"font_color": "blue", "underline": 1})
        self.index = None
        self.index_row = 0
        if with_index:
            self.index = self.wb.add_worksheet(self.names.take("Index"))
            headers = ["Table", "Type", "Rows", "Columns", "Sheet(s)", "JSON file", "Note"]
            for c, h in enumerate(headers):
                self.index.write_string(0, c, h, self.fmt_header)
            self.index.set_column(0, 0, 40)
            self.index.set_column(1, 3, 10)
            self.index.set_column(4, 5, 40)
            self.index.set_column(6, 6, 60)
            self.index.freeze_panes(1, 0)

    # -- table writing ----------------------------------------------------
    def start_table(self, table: TableInfo, columns: list[str]) -> "_SheetCursor":
        return _SheetCursor(self, table, columns)

    def add_index_row(self, table: TableInfo) -> None:
        if self.index is None:
            return
        self.index_row += 1
        r = self.index_row
        self.index.write_string(r, 0, table.full_name)
        self.index.write_string(r, 1, table.kind)
        self.index.write_number(r, 2, table.row_count)
        self.index.write_number(r, 3, len(table.columns))
        if table.excel_sheets:
            first = table.excel_sheets[0].replace("'", "''")
            label = ", ".join(table.excel_sheets)
            self.index.write_url(r, 4, f"internal:'{first}'!A1", self.fmt_link, label)
        if table.json_file:
            self.index.write_string(r, 5, table.json_file)
        notes = []
        if table.error:
            notes.append(f"ERROR: {table.error}")
        if table.row_count == 0 and not table.error:
            notes.append("empty")
        if table.truncated_cells:
            notes.append(
                f"{table.truncated_cells} cell(s) longer than {EXCEL_MAX_CELL_CHARS} "
                "characters were truncated in Excel (full values are in the JSON)"
            )
        if notes:
            self.index.write_string(r, 6, "; ".join(notes))

    def close(self) -> None:
        if self.index is not None and self.index_row:
            self.index.autofilter(0, 0, self.index_row, 6)
        self.wb.close()


class _SheetCursor:
    def __init__(self, book: ExcelWorkbook, table: TableInfo, columns: list[str]):
        self.book = book
        self.table = table
        self.columns = columns
        self.widths = [min(max(len(c) + 2, 8), 60) for c in columns]
        self.sheet = None
        self.row = 0
        self.part = 0
        self._new_sheet()

    def _new_sheet(self) -> None:
        self._finish_sheet()
        self.part += 1
        base = sheet_name_for(self.table)
        if self.part > 1:
            suffix = f" ({self.part})"
            base = base[: EXCEL_MAX_SHEET_NAME - len(suffix)] + suffix
        name = self.book.names.take(base)
        self.table.excel_sheets.append(name)
        self.sheet = self.book.wb.add_worksheet(name)
        for c, col in enumerate(self.columns):
            self.sheet.write_string(0, c, col, self.book.fmt_header)
        self.sheet.freeze_panes(1, 0)
        self.row = 0

    def _finish_sheet(self) -> None:
        if self.sheet is None:
            return
        for c, w in enumerate(self.widths):
            self.sheet.set_column(c, c, w)
        if self.columns:
            self.sheet.autofilter(0, 0, max(self.row, 1), len(self.columns) - 1)

    def write(self, values: tuple) -> None:
        if self.row >= self.book.max_data_rows:
            self._new_sheet()
        self.row += 1
        for c, value in enumerate(values):
            if value is not None:
                self._write_cell(self.row, c, value)

    def _write_cell(self, r: int, c: int, value: Any) -> None:
        ws, book = self.sheet, self.book
        if isinstance(value, bool):
            ws.write_boolean(r, c, value)
        elif isinstance(value, int):
            if abs(value) < EXCEL_MAX_SAFE_INT:
                ws.write_number(r, c, value)
            else:
                ws.write_string(r, c, str(value))
        elif isinstance(value, float):
            if math.isfinite(value):
                ws.write_number(r, c, value)
            else:
                ws.write_string(r, c, str(value))
        elif isinstance(value, Decimal):
            num = _decimal_to_number(value)
            if isinstance(num, str):
                ws.write_string(r, c, num)
            else:
                ws.write_number(r, c, num)
        elif isinstance(value, str):
            self._write_text(r, c, value)
        elif isinstance(value, dt.datetime):
            if value.year < 1900:
                self._write_text(r, c, value.isoformat())
            else:
                ws.write_datetime(r, c, value.replace(tzinfo=None), book.fmt_datetime)
                self._grow(c, 19)
        elif isinstance(value, dt.date):
            if value.year < 1900:
                self._write_text(r, c, value.isoformat())
            else:
                ws.write_datetime(r, c, dt.datetime(value.year, value.month, value.day),
                                  book.fmt_date)
                self._grow(c, 10)
        elif isinstance(value, dt.time):
            ws.write_datetime(r, c, value.replace(tzinfo=None), book.fmt_time)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
            if len(data) <= 16:
                self._write_text(r, c, "0x" + data.hex().upper())
            else:
                ws.write_string(r, c, f"<binary {len(data):,} bytes - see JSON (base64)>")
        else:
            self._write_text(r, c, str(value))

    def _write_text(self, r: int, c: int, text: str) -> None:
        if len(text) > EXCEL_MAX_CELL_CHARS:
            text = text[: EXCEL_MAX_CELL_CHARS - len(TRUNCATION_MARK)] + TRUNCATION_MARK
            self.table.truncated_cells += 1
        self.sheet.write_string(r, c, text)
        self._grow(c, len(text))

    def _grow(self, c: int, length: int) -> None:
        if length + 2 > self.widths[c]:
            self.widths[c] = min(length + 2, 60)

    def close(self) -> None:
        self._finish_sheet()


# --------------------------------------------------------------------------
# Main export routine
# --------------------------------------------------------------------------

@dataclass
class ExportOptions:
    excel: bool = True
    json: bool = True
    excel_layout: str = "single"        # "single" workbook or one "per-table"
    json_layout: str = "per-table"      # "per-table", "single" or "both"
    jsonl: bool = False                 # JSON Lines instead of JSON arrays (per-table files)
    include_empty: bool = False         # write sheets/files for tables with no rows
    include_views: bool = False
    schemas: list[str] | None = None
    tables: list[str] | None = None
    batch_size: int = 5000
    max_rows_per_sheet: int = EXCEL_MAX_ROWS - 1


def export_database(
    engine: sa.Engine,
    out_dir: Path,
    database_label: str,
    options: ExportOptions | None = None,
    progress: Progress = print,
) -> dict:
    """Export all tables. Returns the manifest (also saved as manifest.json)."""
    opts = options or ExportOptions()
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_label = _FILE_BAD_CHARS.sub("_", database_label) or "database"

    tables = list_tables(engine, opts.schemas, opts.include_views, opts.tables)
    progress(f"Found {len(tables)} table(s) to export.")

    json_dir = out_dir / "json"
    excel_dir = out_dir / "excel"
    per_table_json = opts.json and opts.json_layout in ("per-table", "both")
    single_json = opts.json and opts.json_layout in ("single", "both")
    if per_table_json or single_json:
        json_dir.mkdir(exist_ok=True)
    if opts.excel:
        excel_dir.mkdir(exist_ok=True)

    json_names = UniqueNames()
    excel_file_names = UniqueNames()
    combined = (
        CombinedJsonWriter(json_dir / f"{safe_label}.json", database_label) if single_json else None
    )
    workbook = None
    if opts.excel and opts.excel_layout == "single":
        workbook = ExcelWorkbook(excel_dir / f"{safe_label}.xlsx", True, opts.max_rows_per_sheet)

    started = dt.datetime.now()
    try:
        for i, table in enumerate(tables, 1):
            progress(f"[{i}/{len(tables)}] {table.full_name} ...")
            try:
                _export_table(engine, table, opts, workbook, combined, json_dir, excel_dir,
                              json_names, excel_file_names)
                progress(f"    {table.row_count:,} row(s)")
            except Exception as exc:  # keep going; record the failure
                table.error = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
                progress(f"    FAILED: {table.error}")
            if workbook:
                workbook.add_index_row(table)
    finally:
        if combined:
            combined.close()
        if workbook:
            progress(f"Writing Excel workbook {workbook.path} ...")
            workbook.close()

    manifest = {
        "database": database_label,
        "exported_at": started.isoformat(timespec="seconds"),
        "duration_seconds": round((dt.datetime.now() - started).total_seconds(), 1),
        "table_count": len(tables),
        "total_rows": sum(t.row_count for t in tables),
        "failed_tables": [t.full_name for t in tables if t.error],
        "excel_workbook": (
            str(workbook.path.relative_to(out_dir)) if workbook else None
        ),
        "combined_json": str(combined.path.relative_to(out_dir)) if combined else None,
        "notes": [
            "Binary values are base64 encoded in JSON.",
            "Dates/times are ISO 8601 strings in JSON.",
            "Decimals that cannot be represented exactly as a number are strings.",
        ],
        "tables": [
            {
                "schema": t.schema,
                "name": t.name,
                "type": t.kind,
                "row_count": t.row_count,
                "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable}
                            for c in t.columns],
                "json_file": t.json_file,
                "excel_file": t.excel_file,
                "excel_sheets": t.excel_sheets,
                "excel_truncated_cells": t.truncated_cells,
                "error": t.error,
            }
            for t in tables
        ],
    }
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return manifest


def _export_table(engine, table, opts, workbook, combined, json_dir, excel_dir,
                  json_names, excel_file_names) -> None:
    sql = _select_sql(engine, table)
    exec_opts = {}
    if engine.dialect.supports_server_side_cursors:
        exec_opts["stream_results"] = True

    with engine.connect() as conn:
        result = conn.execution_options(**exec_opts).execute(sa.text(sql))
        columns = list(result.keys())
        if not table.columns:
            table.columns = [Column(c, "") for c in columns]

        json_writer = sheet = own_book = None
        stem = file_stem_for(table)
        count = 0

        def open_writers() -> None:
            nonlocal json_writer, sheet, own_book
            if opts.json and opts.json_layout in ("per-table", "both"):
                name = json_names.take(stem) + (".jsonl" if opts.jsonl else ".json")
                json_writer = JsonTableWriter(json_dir / name, opts.jsonl)
                table.json_file = f"json/{name}"
            if opts.excel:
                book = workbook
                if book is None:
                    name = excel_file_names.take(stem) + ".xlsx"
                    own_book = book = ExcelWorkbook(excel_dir / name, False,
                                                    opts.max_rows_per_sheet)
                    table.excel_file = f"excel/{name}"
                else:
                    table.excel_file = f"excel/{book.path.name}"
                sheet = book.start_table(table, columns)
            if combined:
                combined.begin_table(table.full_name)

        opened = False
        if opts.include_empty:
            open_writers()
            opened = True
        try:
            for batch in result.partitions(opts.batch_size):
                if not opened:
                    open_writers()
                    opened = True
                for row in batch:
                    values = tuple(row)
                    if json_writer or combined:
                        obj = {k: to_json_value(v) for k, v in zip(columns, values)}
                        if json_writer:
                            json_writer.write(obj)
                        if combined:
                            combined.write(obj)
                    if sheet:
                        sheet.write(values)
                    count += 1
        finally:
            table.row_count = count
            if json_writer:
                json_writer.close()
            if sheet:
                sheet.close()
            if own_book:
                own_book.close()
            if combined and opened:
                combined.end_table()
