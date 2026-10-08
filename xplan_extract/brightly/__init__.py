"""Export Xplan data in Brightly's record shape (see the Brightly Xplan import brief).

This module holds the parts that don't depend on how Xplan lays out its tables:

* ``FactFind``      - builds ``ff = {a: {...}, n: {...}}`` using the ids, scopes, repeat groups and
                      options in ``factfind_schema.json``; anything that doesn't fit is reported as
                      unmapped instead of guessed.
* ``scrub_tfns``    - finds and removes tax file numbers anywhere in a record (ATO check digit).
* ``ExportWriter``  - writes households/prospects/entities/tasks as JSON Lines, ``unmapped.csv``,
                      ``manifest.json`` and ``README.md``.

The Xplan-specific queries live in ``xplan_map.py``.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

# Brightly's fact-find definition is not copied into this repository (it belongs to the
# Brightly repo). Point BRIGHTLY_SCHEMA or --schema at factfind_schema.json from the handoff.
SCHEMA_ENV = "BRIGHTLY_SCHEMA"

YES = {"y", "yes", "true", "1", "t"}
NO = {"n", "no", "false", "0", "f"}
OWNER_VALUES = {"1": "1", "2": "2", "joint": "joint", "person 1": "1", "person 2": "2",
                "client": "1", "partner": "2", "j": "joint", "both": "joint"}


# --------------------------------------------------------------------------
# Tax file numbers
# --------------------------------------------------------------------------

_TFN_WEIGHTS = {9: (1, 4, 3, 7, 5, 8, 6, 9, 10), 8: (10, 7, 8, 4, 6, 3, 5, 1)}
_TFN_CANDIDATE = re.compile(r"(?<![\d])(\d{3}[ -]?\d{3}[ -]?\d{2,3})(?![\d])")
TFN_MARK = "[TFN removed]"


def is_tfn(digits: str) -> bool:
    """True when the digits pass the ATO tax file number check digit."""
    digits = re.sub(r"\D", "", digits)
    weights = _TFN_WEIGHTS.get(len(digits))
    if not weights or len(set(digits)) == 1:
        return False
    return sum(int(d) * w for d, w in zip(digits, weights)) % 11 == 0


def scrub_tfns(value: Any, counter: list[int]) -> Any:
    """Return ``value`` with every TFN-looking number replaced; counts removals in counter[0]."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            if is_tfn(m.group(1)):
                counter[0] += 1
                return TFN_MARK
            return m.group(1)
        return _TFN_CANDIDATE.sub(repl, value)
    if isinstance(value, dict):
        return {k: scrub_tfns(v, counter) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tfns(v, counter) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and is_tfn(str(value)):
        counter[0] += 1
        return None
    return value


TFN_COLUMN = re.compile(r"(^|[^a-z])tfn|tax_?file", re.I)


def is_tfn_column(name: str) -> bool:
    return bool(TFN_COLUMN.search(name or ""))


# --------------------------------------------------------------------------
# Value cleaning
# --------------------------------------------------------------------------

def clean_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        if value.year < 1850:
            return None
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", text):
        text = text[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y",
                "%d %B %Y"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d") if parsed.year >= 1850 else None
    return None


def clean_datetime(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone(dt.timedelta(hours=10)))
        return value.isoformat(timespec="seconds")
    d = clean_date(value)
    return f"{d}T00:00:00+10:00" if d else None


def clean_number(value: Any) -> int | float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        num = float(value)
    else:
        text = re.sub(r"[,$\s]", "", str(value))
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        try:
            num = float(text)
        except ValueError:
            return None
    return int(num) if num == int(num) else round(num, 2)


def clean_yesno(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in YES:
        return "Yes"
    if text in NO:
        return "No"
    return None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[ \t]+", " ", str(value)).strip()
    return text or None


# --------------------------------------------------------------------------
# Fact find
# --------------------------------------------------------------------------

class Schema:
    def __init__(self, path: Path | str | None = None):
        import os

        path = path or os.environ.get(SCHEMA_ENV)
        if not path or not Path(path).is_file():
            raise FileNotFoundError(
                "Brightly fact-find schema not found. Set BRIGHTLY_SCHEMA in .env (or pass "
                "--schema) to the factfind_schema.json from the Brightly handoff.")
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = raw.get("version")
        self.fields: dict[str, dict] = {f["id"]: f for f in raw["fields"]}
        self.repeat: dict[str, dict] = raw["repeat"]
        rp = raw["riskProfile"]
        for q in rp["questions"]:
            self.fields.setdefault(q["id"], {**q, "scope": rp.get("scope", "person"),
                                             "section": "riskProfile"})
        self.fields.setdefault(rp["decision"]["id"], {**rp["decision"], "scope": "person",
                                                      "section": "riskProfile"})
        self.fields.setdefault(rp["household"]["id"], {**rp["household"], "scope": "shared",
                                                       "section": "riskProfile"})

    def is_sensitive(self, fid: str) -> bool:
        return bool(self.fields.get(fid, {}).get("sensitive"))


@dataclass
class Unmapped:
    table: str
    field: str
    sample_count: int
    suggested_target: str
    reason: str = ""


class FactFind:
    """Collects ``ff.a`` answers, converting and checking every value against the schema."""

    def __init__(self, schema: Schema, problems: list[Unmapped] | None = None):
        self.schema = schema
        self.a: dict[str, Any] = {}
        self.n: dict[str, int] = {}
        self.problems = problems if problems is not None else []
        self.sensitive_count = 0

    def set(self, fid: str, value: Any, person: int | None = None, group: str | None = None,
            index: int | None = None, owner: str | None = None, source: str = "") -> bool:
        f = self.schema.fields.get(fid)
        if f is None:
            self._problem(source, fid, "no such fact-find field")
            return False
        converted = self._convert(f, value)
        if converted is None:
            if value not in (None, ""):
                self._problem(source, fid, f"value doesn't fit type {f['type']}"
                              + (f" / options {f.get('options')}" if f.get("options") else ""))
            return False
        rep = f.get("repeat")
        if rep:
            if group != rep or index is None:
                self._problem(source, fid, f"needs repeat group {rep}")
                return False
            if index >= self.schema.repeat[rep]["max"]:
                self._problem(source, fid, f"more than {self.schema.repeat[rep]['max']} {rep}s")
                return False
            key = f"{rep}#{index}.{fid}"
            self.n[rep] = max(self.n.get(rep, 0), index + 1)
        elif f.get("scope") == "person":
            if person not in (1, 2):
                self._problem(source, fid, "per-person field without person 1/2")
                return False
            key = f"{fid}@{person}"
        else:
            key = fid
        self.a[key] = converted
        if f.get("sensitive"):
            self.sensitive_count += 1
        if owner and f.get("owner"):
            own = OWNER_VALUES.get(str(owner).strip().lower())
            if own:
                self.a[f"{key}~owner"] = own
        return True

    def set_repeat_person(self, group: str, index: int, person: int) -> None:
        """Whose fund/property it is: ``fund#0.person`` = "1" or "2"."""
        self.a[f"{group}#{index}.person"] = str(person)
        self.n[group] = max(self.n.get(group, 0), index + 1)

    def _convert(self, f: dict, value: Any) -> Any:
        t = f["type"]
        if value is None or value == "":
            return None
        if t in ("money", "number", "percent", "age"):
            return clean_number(value)
        if t == "year":
            num = clean_number(value)
            return str(int(num)) if num and 1900 <= num <= 2100 else None
        if t == "date":
            return clean_date(value)
        if t == "yesno":
            return clean_yesno(value)
        if t == "select":
            return self._option(f, value)
        if t == "multiselect":
            parts = value if isinstance(value, list) else re.split(r"[;,|]", str(value))
            chosen = [self._option(f, p) for p in parts if str(p).strip()]
            return chosen if chosen and all(chosen) else None
        return clean_text(value)

    @staticmethod
    def _option(f: dict, value: Any) -> str | None:
        text = str(value).strip().lower()
        for opt in f.get("options", []):
            if opt.lower() == text:
                return opt
        return None

    def _problem(self, source: str, fid: str, reason: str) -> None:
        table, _, column = source.partition(".") if "." in source else ("", "", source)
        self.problems.append(Unmapped(table or "(derived)", column or source, 1, fid, reason))

    def as_dict(self) -> dict:
        return {"a": dict(sorted(self.a.items())), "n": dict(sorted(self.n.items()))}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

FILES = ("family_groups", "prospects", "entities", "tasks")


@dataclass
class ExportWriter:
    out_dir: Path
    source: str
    schema_version: str | None = None
    counts: dict[str, int] = field(default_factory=lambda: {f: 0 for f in FILES})
    tfn_removed: list[int] = field(default_factory=lambda: [0])
    tfn_fields_dropped: int = 0
    sensitive_values: int = 0
    unmapped: dict[tuple[str, str], Unmapped] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._fh = {f: open(self.out_dir / f"{f}.jsonl", "w", encoding="utf-8", newline="\n")
                    for f in FILES}

    def write(self, kind: str, record: dict) -> None:
        record = scrub_tfns(record, self.tfn_removed)
        self._fh[kind].write(json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                             + "\n")
        self.counts[kind] += 1

    def add_unmapped(self, items: list[Unmapped]) -> None:
        for u in items:
            key = (u.table, u.field)
            if key in self.unmapped:
                self.unmapped[key].sample_count += u.sample_count
            else:
                self.unmapped[key] = Unmapped(u.table, u.field, u.sample_count,
                                              u.suggested_target, u.reason)

    def close(self, readme: str) -> dict:
        for fh in self._fh.values():
            fh.close()
        with open(self.out_dir / "unmapped.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["xplan_table", "xplan_field", "sample_count", "suggested_target",
                        "reason"])
            for u in sorted(self.unmapped.values(), key=lambda u: (u.table, u.field)):
                w.writerow([u.table, u.field, u.sample_count, u.suggested_target, u.reason])
        manifest = {
            "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "xplan_source": self.source,
            "factfind_schema": self.schema_version,
            "record_counts": {f"{k}.jsonl": v for k, v in self.counts.items()},
            "unmapped_fields": len(self.unmapped),
            "tfn_fields_dropped": self.tfn_fields_dropped,
            "tfn_values_removed_from_text": self.tfn_removed[0],
            "sensitive_health_values": self.sensitive_values,
        }
        (self.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                                    encoding="utf-8")
        (self.out_dir / "README.md").write_text(readme, encoding="utf-8")
        return manifest
