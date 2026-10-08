"""Load a Brightly export (households/prospects/entities/tasks JSON Lines) into the database
(step 3.3). A household record in the export is a family group in the database and in Brightly;
the export keeps Brightly's interchange names (households.jsonl, H- ids).

    stage    every line goes into staging_record with a new import_run
    validate counts match manifest.json; ids, references and dates hold; no TFNs anywhere
    load     one transaction: upsert family groups/entities/tasks by id, rebuild their
             source='xplan' child rows, append change_log entries

Safe to re-run: records are matched by id, and child rows added in Brightly (source
'brightly') are never touched. A family group (household record) or entity edited in Brightly since its last import
(updated_at > imported_at) is left alone and reported, unless overwrite_edited=True.
Problems name record ids only, never client names.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import sqlalchemy as sa

from . import scrub_tfns
from . import database as db

Progress = Callable[[str], None]
FILES = {"household": "households.jsonl", "prospect": "prospects.jsonl",
         "entity": "entities.jsonl", "task": "tasks.jsonl"}
PREFIX = {"household": "H-", "prospect": "H-", "entity": "E-", "task": "T-"}
BATCH = 500


class LoadError(Exception):
    pass


@dataclass
class Result:
    run_id: int | None = None
    status: str = ""
    counts: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# -- reading -------------------------------------------------------------------------------

def read_export(export_dir: Path) -> tuple[dict, dict[str, list[dict]]]:
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.exists():
        raise LoadError(f"No manifest.json in {export_dir} - is this a Brightly export folder?")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records: dict[str, list[dict]] = {}
    for kind, name in FILES.items():
        path = export_dir / name
        rows = []
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if line.strip():
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError as exc:
                            raise LoadError(f"{name} line {n} is not valid JSON: {exc.msg}")
        records[kind] = rows
    return manifest, records


# -- validation ----------------------------------------------------------------------------

def _date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    return dt.date.fromisoformat(str(value)[:10])


def _stamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    s = str(value).replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _has_tfn(record: dict) -> bool:
    """Same test the exporter applies (ATO check digit on 8-9 digit numbers, in text and as
    plain numbers), so a clean export always passes and a hand-edited file can't sneak one in."""
    hits = [0]
    scrub_tfns(record, hits)
    return hits[0] > 0


DATE_FIELDS = {"household": ["since", "lastReview", "nextReview", "ofa", "insRenewal", "added"],
               "prospect": ["since", "lastReview", "nextReview", "ofa", "insRenewal", "added"],
               "entity": [], "task": ["due"]}


def validate(manifest: dict, records: dict[str, list[dict]], existing_households: set[str],
             file_counts: dict[str, int] | None = None) -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    expected = manifest.get("record_counts", {})
    file_counts = file_counts or {k: len(v) for k, v in records.items()}
    for kind, name in FILES.items():
        if name in expected and expected[name] != file_counts[kind]:
            problems.append(f"{name}: manifest says {expected[name]} records, file has "
                            f"{file_counts[kind]}")
    seen: dict[str, str] = {}
    xplan_seen: dict[str, str] = {}
    households = {r.get("id") for k in ("household", "prospect") for r in records[k]}
    known = households | existing_households
    for kind, rows in records.items():
        for n, r in enumerate(rows, 1):
            rid = r.get("id")
            where = f"{FILES[kind]} line {n}"
            if not isinstance(rid, str) or not rid.startswith(PREFIX[kind]):
                problems.append(f"{where}: id missing or not starting {PREFIX[kind]}")
                continue
            if rid in seen:
                problems.append(f"{rid}: duplicate id (also in {seen[rid]})")
            seen[rid] = where
            xp = (r.get("ext") or {}).get("xplan")
            if xp and kind != "task":
                key = ("H" if kind in ("household", "prospect") else "E") + str(xp)
                if key in xplan_seen:
                    problems.append(f"{rid}: Xplan id also used by {xplan_seen[key]}")
                xplan_seen[key] = rid
            for f in DATE_FIELDS[kind]:
                try:
                    _date(r.get(f))
                except ValueError:
                    problems.append(f"{rid}: {f} is not a YYYY-MM-DD date")
            if _has_tfn(r):
                problems.append(f"{rid}: contains a number that passes the TFN check")
            if kind in ("household", "prospect"):
                people = (r.get("profile") or {}).get("people") or []
                want = 2 if r.get("type") == "Couple" else 1
                if not people:
                    problems.append(f"{rid}: no people")
                elif len(people) != want:
                    warnings.append(f"{rid}: type {r.get('type')} but {len(people)} people")
                if not r.get("name"):
                    problems.append(f"{rid}: no name")
            elif kind == "entity":
                if r.get("home") and r["home"] not in known:
                    problems.append(f"{rid}: home family group {r['home']} not found")
                for role in r.get("roles") or []:
                    if role.get("c") not in known:
                        problems.append(f"{rid}: role links unknown family group {role.get('c')}")
                if not r.get("name"):
                    problems.append(f"{rid}: no name")
            elif kind == "task":
                if r.get("c") and r["c"] not in known:
                    warnings.append(f"{rid}: family group {r['c']} not found; task loads unlinked")
    return problems, warnings


# -- loading -------------------------------------------------------------------------------

def redirect_merged(records: dict[str, list[dict]], merged: dict[str, str]) -> list[str]:
    """Records merged away in Brightly stay merged: skip them and point links at the record
    they were merged into."""
    def final(i):
        seen = set()
        while i in merged and i not in seen:
            seen.add(i)
            i = merged[i]
        return i
    notes = []
    for kind in ("household", "prospect", "entity"):
        keep = []
        for r in records[kind]:
            if r.get("id") in merged:
                notes.append(f"{r['id']}: merged into {final(r['id'])} in Brightly; not re-imported")
            else:
                keep.append(r)
        records[kind] = keep
    for e in records["entity"]:
        if e.get("home"):
            e["home"] = final(e["home"])
        for ro in e.get("roles") or []:
            ro["c"] = final(ro.get("c"))
        for a in e.get("accts") or []:
            a["c"] = final(a.get("c"))
        if e.get("primary") and ":" in e["primary"]:
            c, pk = e["primary"].rsplit(":", 1)
            e["primary"] = f"{final(c)}:{pk}"
    for t in records["task"]:
        if t.get("c"):
            t["c"] = final(t["c"])
    return notes


def _num(v: Any):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _household_row(r: dict, now, run_id) -> dict:
    profile = r.get("profile") or {}
    ff = r.get("ff") or {}
    return {"id": r["id"], "xplan_id": (r.get("ext") or {}).get("xplan"),
            "is_prospect": bool(r.get("prospect")), "status": r.get("status"),
            "name": r["name"], "type": r.get("type"), "adviser": r.get("adviser"),
            "since": _date(r.get("since")), "email": r.get("email"), "phone": r.get("phone"),
            "city": r.get("city"), "notes": r.get("notes") or None,
            "last_review": _date(r.get("lastReview")), "next_review": _date(r.get("nextReview")),
            "ofa": _date(r.get("ofa")), "ins_renewal": _date(r.get("insRenewal")),
            "fee": _num(r.get("fee")), "fee_type": r.get("feeType"), "risk": profile.get("risk"),
            "prospect_source": r.get("source"), "prospect_added": _date(r.get("added")),
            "ff_answers": ff.get("a") or {}, "ff_counts": ff.get("n") or {},
            "imported_at": now, "updated_at": now, "import_run_id": run_id}


def _household_children(r: dict) -> dict[sa.Table, list[dict]]:
    hid, p = r["id"], r.get("profile") or {}
    rows: dict[sa.Table, list[dict]] = {t: [] for t in db.FAMILY_GROUP_CHILDREN}
    rows[db.person] = [
        {"id": f"{hid}:{i}", "family_group_id": hid, "position": i, "name": x.get("n"),
         "role": x.get("role"), "dob": _date(x.get("dob")), "occupation": x.get("job"),
         "income": _num(x.get("income"))}
        for i, x in enumerate(p.get("people") or [], 1)]
    c = r.get("contacts") or {}
    rows[db.contact_point] = (
        [{"family_group_id": hid, "kind": "email", "value": e["addr"], "is_primary": bool(e.get("primary"))}
         for e in c.get("emails") or [] if e.get("addr")] +
        [{"family_group_id": hid, "kind": "phone", "value": ph["num"], "is_primary": bool(ph.get("primary"))}
         for ph in c.get("phones") or [] if ph.get("num")])
    rows[db.account] = [
        {"id": f"{hid}:{a.get('id')}", "family_group_id": hid, "source_ref": a.get("id"),
         "platform": a.get("p"), "product": a.get("prod"), "kind": a.get("kind"),
         "owner": a.get("owner"), "balance": _num(a.get("bal")), "as_at": _date(a.get("asAt")),
         "member_no": a.get("member"), "insurance": a.get("ins") or None}
        for a in p.get("accounts") or []]
    for kind, key in (("asset", "assets"), ("liability", "liabs")):
        rows[db.asset_liability] += [
            {"family_group_id": hid, "kind": kind, "name": x[0] if len(x) > 0 else None,
             "owner": x[1] if len(x) > 1 else None, "value": _num(x[2]) if len(x) > 2 else None}
            for x in p.get(key) or []]
    meta = p.get("goalMeta") or []
    rows[db.goal] = [
        {"family_group_id": hid, "position": i, "text": g,
         "date": _date((meta[i] if i < len(meta) else {}).get("date")),
         "origin": (meta[i] if i < len(meta) else {}).get("source")}
        for i, g in enumerate(p.get("goals") or []) if g]
    rows[db.advice_history] = [
        {"family_group_id": hid, "date": _date(a.get("date")), "document": a.get("document"),
         "scope": a.get("scope"), "summary": a.get("summary")} for a in p.get("adviceHist") or []]
    rows[db.file_note] = [
        {"family_group_id": hid, "at": _stamp(n.get("at")), "author": n.get("by"),
         "title": n.get("title"), "text": n.get("text")} for n in r.get("fileNotes") or []]
    rows[db.signed_document] = [
        {"family_group_id": hid, "doc_key": k, "date": _date(v.get("date")), "signed_by": v.get("by"),
         "note": v.get("note")} for k, v in (r.get("signed") or {}).items() if isinstance(v, dict)]
    return rows


def _entity_row(r: dict, now, run_id) -> dict:
    ff = dict(r.get("ff") or {})
    s = r.get("strategy") or {}
    return {"id": r["id"], "xplan_id": (r.get("ext") or {}).get("xplan"), "type": r.get("type"),
            "name": r["name"], "status": r.get("status"), "home_family_group_id": r.get("home"),
            "primary_ref": r.get("primary"), "abn": ff.pop("abn", None),
            "tfn_held": ff.pop("tfnHeld", None), "details": ff,
            "strategy_profile": s.get("profile"), "strategy_reviewed": _date(s.get("reviewed")),
            "strategy_note": s.get("note") or None,
            "imported_at": now, "updated_at": now, "import_run_id": run_id}


def _edited(conn, table: sa.Table, ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        q = sa.select(table.c.id, table.c.imported_at, table.c.updated_at).where(
            table.c.id.in_(chunk))
        for rid, imp, upd in conn.execute(q):
            if imp and upd and upd > imp:
                out.add(rid)
    return out


def _existing(conn, table: sa.Table, ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), BATCH):
        out |= {r[0] for r in conn.execute(sa.select(table.c.id).where(
            table.c.id.in_(ids[i:i + BATCH])))}
    return out


def _upsert(conn, table: sa.Table, rows: list[dict], existing: set[str]) -> None:
    new = [r for r in rows if r["id"] not in existing]
    old = [r for r in rows if r["id"] in existing]
    for i in range(0, len(new), BATCH):
        conn.execute(table.insert(), new[i:i + BATCH])
    for r in old:
        conn.execute(table.update().where(table.c.id == r["id"]).values(**r))


def load_export(engine: sa.Engine, export_dir: Path, *, dry_run: bool = False,
                overwrite_edited: bool = False, progress: Progress = print) -> Result:
    export_dir = Path(export_dir)
    manifest, records = read_export(export_dir)
    res = Result()
    now = dt.datetime.now(dt.timezone.utc)
    with engine.connect() as conn:
        existing_h = {r[0] for r in conn.execute(sa.select(db.family_group.c.id))}
        merged = {r.dropped_id: r.kept_id for r in conn.execute(
            sa.select(db.merge_record.c.dropped_id, db.merge_record.c.kept_id))}
    file_counts = {k: len(v) for k, v in records.items()}
    progress("Records: " + ", ".join(f"{len(v):,} {k}" for k, v in records.items()))

    # stage
    with engine.begin() as conn:
        res.run_id = conn.execute(db.import_run.insert().values(
            started_at=now, status="staged", source=manifest.get("xplan_source"),
            export_exported_at=manifest.get("exported_at"), manifest=manifest)
            .returning(db.import_run.c.id)).scalar_one() \
            if engine.dialect.insert_returning else None
        if res.run_id is None:
            conn.execute(db.import_run.insert().values(
                started_at=now, status="staged", source=manifest.get("xplan_source"),
                export_exported_at=manifest.get("exported_at"), manifest=manifest))
            res.run_id = conn.execute(sa.select(sa.func.max(db.import_run.c.id))).scalar_one()
        rows = [{"run_id": res.run_id, "kind": k, "line_no": n, "record_id": r.get("id"),
                 "payload": json.dumps(r, ensure_ascii=False)}
                for k, rs in records.items() for n, r in enumerate(rs, 1)]
        for i in range(0, len(rows), BATCH):
            conn.execute(db.staging_record.insert(), rows[i:i + BATCH])
    progress(f"Staged as import run {res.run_id}.")

    # validate
    redirected = redirect_merged(records, merged) if merged else []
    res.problems, res.warnings = validate(manifest, records, existing_h, file_counts)
    res.warnings = redirected + res.warnings
    for w in res.warnings[:20]:
        progress(f"  warning: {w}")
    if res.problems:
        res.status = "failed"
        with engine.begin() as conn:
            conn.execute(db.import_run.update().where(db.import_run.c.id == res.run_id).values(
                status="failed", finished_at=dt.datetime.now(dt.timezone.utc),
                problems=res.problems[:1000]))
        return res
    if dry_run:
        res.status = "dry-run"
        with engine.begin() as conn:
            conn.execute(db.import_run.update().where(db.import_run.c.id == res.run_id).values(
                status="dry-run", finished_at=dt.datetime.now(dt.timezone.utc),
                problems=res.warnings[:1000]))
        return res

    # load
    with engine.begin() as conn:
        hh = records["household"] + records["prospect"]
        skip = set() if overwrite_edited else _edited(conn, db.family_group, [r["id"] for r in hh])
        skip |= set() if overwrite_edited else _edited(
            conn, db.entity, [r["id"] for r in records["entity"]])
        if skip:
            res.warnings.append(f"{len(skip)} record(s) edited in Brightly since the last import "
                                f"were left alone: {', '.join(sorted(skip)[:20])}")
        hh = [r for r in hh if r["id"] not in skip]
        ids = [r["id"] for r in hh]
        existing = _existing(conn, db.family_group, ids)
        _upsert(conn, db.family_group, [_household_row(r, now, res.run_id) for r in hh], existing)
        res.counts["family_groups"] = len(hh)

        children: dict[sa.Table, list[dict]] = {t: [] for t in [db.person, *db.FAMILY_GROUP_CHILDREN]}
        for r in hh:
            for t, rows in _household_children(r).items():
                children[t] += rows
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            for t in db.FAMILY_GROUP_CHILDREN:
                conn.execute(t.delete().where(t.c.family_group_id.in_(chunk), t.c.source == "xplan"))
        # people: keep ids stable for entity_role links; upsert then drop extras
        pexist = _existing(conn, db.person, [p["id"] for p in children[db.person]])
        _upsert(conn, db.person, children[db.person], pexist)
        for t in db.FAMILY_GROUP_CHILDREN:
            rows = children[t]
            for i in range(0, len(rows), BATCH):
                conn.execute(t.insert(), rows[i:i + BATCH])
            res.counts[t.name] = len(rows)
        res.counts["person"] = len(children[db.person])

        ents = [r for r in records["entity"] if r["id"] not in skip]
        eids = [r["id"] for r in ents]
        _upsert(conn, db.entity, [_entity_row(r, now, res.run_id) for r in ents],
                _existing(conn, db.entity, eids))
        for i in range(0, len(eids), BATCH):
            conn.execute(db.entity_role.delete().where(
                db.entity_role.c.entity_id.in_(eids[i:i + BATCH]), db.entity_role.c.source == "xplan"))
        roles = []
        for r in ents:
            for ro in r.get("roles") or []:
                pk = str(ro.get("pk") or "")
                roles.append({"entity_id": r["id"], "family_group_id": ro["c"], "person_pos": pk or None,
                              "person_id": f"{ro['c']}:{pk}" if pk else None,
                              "roles": ro.get("roles") or [], "member_balance": _num(ro.get("bal")),
                              "is_primary": r.get("primary") == f"{ro['c']}:{pk}"})
        pids = _existing(conn, db.person, [x["person_id"] for x in roles if x["person_id"]])
        for x in roles:
            if x["person_id"] not in pids:
                x["person_id"] = None
        for i in range(0, len(roles), BATCH):
            conn.execute(db.entity_role.insert(), roles[i:i + BATCH])
        for r in ents:
            for a in r.get("accts") or []:
                conn.execute(db.account.update().where(
                    db.account.c.id == f"{a.get('c')}:{a.get('id')}").values(entity_id=r["id"]))
        res.counts["entities"] = len(ents)
        res.counts["entity_role"] = len(roles)

        known = set(ids) | {r[0] for r in conn.execute(sa.select(db.family_group.c.id))}
        trows = [{"id": t["id"], "family_group_id": t.get("c") if t.get("c") in known else None,
                  "text": t.get("t") or "Task", "who": t.get("who"), "due": _date(t.get("due")),
                  "done": bool(t.get("done")), "status": t.get("status"),
                  "imported_at": now, "updated_at": now} for t in records["task"]]
        _upsert(conn, db.task, trows, _existing(conn, db.task, [t["id"] for t in trows]))
        res.counts["tasks"] = len(trows)

        logs = []
        for r in hh + ents:
            for e in r.get("log") or [{"at": now.isoformat(), "by": "Xplan import",
                                       "kind": "Imported", "m": "Imported from Xplan"}]:
                logs.append({"at": _stamp(e.get("at")) or now, "actor": e.get("by"),
                             "record_type": "entity" if r["id"].startswith("E-") else "family_group",
                             "record_id": r["id"], "kind": e.get("kind"),
                             "message": f"{e.get('m') or ''} (import run {res.run_id})".strip()})
        for i in range(0, len(logs), BATCH):
            conn.execute(db.change_log.insert(), logs[i:i + BATCH])

        conn.execute(db.import_run.update().where(db.import_run.c.id == res.run_id).values(
            status="loaded", finished_at=dt.datetime.now(dt.timezone.utc),
            counts=dict(res.counts), problems=res.warnings[:1000]))
    res.status = "loaded"
    return res


def table_counts(engine: sa.Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t.name: conn.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                for t in db.metadata.sorted_tables if t.name not in ("staging_record",)}
