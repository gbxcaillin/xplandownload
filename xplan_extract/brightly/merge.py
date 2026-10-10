"""Find and merge duplicate family groups or entities.

    find_candidates(engine, kind)          likely duplicate pairs, with the reasons
    compare(engine, kind, a_id, b_id)      every field side by side: same / only A / only B / conflict
    apply_merge(engine, kind, keep, drop, picks, exclude, actor)

Merge rules, field by field (a "field" includes each person's details and each fact-find
answer):
  1. a field ticked "exclude" keeps the kept record's value as it is, even if that is blank;
  2. a field with a pick takes the picked record's value (used for conflicts);
  3. otherwise a filled value beats a blank one, and on a conflict the kept record wins.
Fact-find repeat groups (funds, dependants ...) are compared as whole groups: keep one side's
items, or combine both lists.

Everything attached to the dropped record (people, contacts, accounts, assets, goals, advice,
file notes, signed documents, tasks, document links, entity roles) moves to the kept one, the
dropped record is deleted, and merge_record keeps a snapshot plus the choices, so the loader
won't recreate the duplicate on a re-import. One transaction: all or nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from . import database as db

KINDS = ("family_group", "entity")

FG_FIELDS = [
    ("name", "Name"), ("type", "Type"), ("status", "Status (Active / Inactive)"),
    ("is_prospect", "Prospect"), ("adviser", "Adviser"), ("since", "Client since"),
    ("email", "Email"), ("phone", "Phone"), ("city", "City"), ("notes", "Notes"),
    ("last_review", "Last review"), ("next_review", "Next review"), ("ofa", "OFA anniversary"),
    ("ins_renewal", "Insurance renewal"), ("fee", "Fee ($ a year)"), ("fee_type", "Fee type"),
    ("risk", "Risk profile"), ("prospect_source", "Prospect source"),
    ("prospect_added", "Prospect added"), ("sharepoint_folder", "SharePoint folder"),
]
PERSON_FIELDS = [("name", "name"), ("role", "role"), ("dob", "date of birth"),
                 ("occupation", "occupation"), ("income", "income ($ a year)")]
ENTITY_FIELDS = [
    ("name", "Name"), ("type", "Type"), ("status", "Status (Active / Inactive)"),
    ("home_family_group_id", "Home family group"), ("primary_ref", "Primary contact"),
    ("abn", "ABN"), ("tfn_held", "TFN held"), ("strategy_profile", "Investment strategy profile"),
    ("strategy_reviewed", "Strategy reviewed"), ("strategy_note", "Strategy note"),
]
REPEAT_KEY = re.compile(r"^([A-Za-z]+)#(\d+)\.(.+)$")


class MergeError(Exception):
    pass


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == [] or v == {}


def _plain(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _same(a: Any, b: Any) -> bool:
    a, b = _plain(a), _plain(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return a == b


def _table(kind: str) -> sa.Table:
    if kind not in KINDS:
        raise MergeError(f"kind must be one of {KINDS}")
    return db.family_group if kind == "family_group" else db.entity


def _row(conn, kind: str, rid: str) -> dict:
    t = _table(kind)
    r = conn.execute(sa.select(t).where(t.c.id == rid)).mappings().first()
    if r is None:
        raise MergeError(f"{rid} not found")
    return dict(r)


def _people(conn, fg_id: str) -> dict[int, dict]:
    return {r["position"]: dict(r) for r in conn.execute(
        sa.select(db.person).where(db.person.c.family_group_id == fg_id)).mappings()}


def _split_ff(answers: dict) -> tuple[dict, dict[str, list[dict]]]:
    """Plain answers, and repeat groups as lists of {field: value} items in index order."""
    plain, groups = {}, defaultdict(dict)
    for k, v in (answers or {}).items():
        m = REPEAT_KEY.match(k)
        if m:
            groups[m.group(1)].setdefault(int(m.group(2)), {})[m.group(3)] = v
        else:
            plain[k] = v
    return plain, {g: [items[i] for i in sorted(items)] for g, items in groups.items()}


def _join_ff(plain: dict, groups: dict[str, list[dict]]) -> tuple[dict, dict]:
    out = dict(plain)
    for g, items in groups.items():
        for i, item in enumerate(items):
            for f, v in item.items():
                out[f"{g}#{i}.{f}"] = v
    return out, {g: len(items) for g, items in groups.items() if items}


# -- values per merge key --------------------------------------------------------------------

def _values(conn, kind: str, rid: str) -> tuple[dict, dict[str, str]]:
    """{merge key: value} and {merge key: label} for one record."""
    r = _row(conn, kind, rid)
    vals, labels = {}, {}
    fields = FG_FIELDS if kind == "family_group" else ENTITY_FIELDS
    for col, label in fields:
        vals[col], labels[col] = r.get(col), label
    if kind == "family_group":
        for pos, p in _people(conn, rid).items():
            for col, label in PERSON_FIELDS:
                key = f"person:{pos}:{col}"
                vals[key], labels[key] = p.get(col), f"Person {pos} {label}"
        plain, groups = _split_ff(r.get("ff_answers") or {})
        for k, v in plain.items():
            vals[f"ff:{k}"], labels[f"ff:{k}"] = v, f"Fact find: {k}"
        for g, items in groups.items():
            vals[f"ffgroup:{g}"], labels[f"ffgroup:{g}"] = items, f"Fact find {g} list"
    else:
        for k, v in (r.get("details") or {}).items():
            vals[f"details:{k}"], labels[f"details:{k}"] = v, f"Details: {k}"
    return vals, labels


CHILD_TABLES = {
    "family_group": [("person", db.person, "family_group_id"),
                     ("contacts", db.contact_point, "family_group_id"),
                     ("accounts", db.account, "family_group_id"),
                     ("assets and liabilities", db.asset_liability, "family_group_id"),
                     ("goals", db.goal, "family_group_id"),
                     ("advice history", db.advice_history, "family_group_id"),
                     ("file notes", db.file_note, "family_group_id"),
                     ("signed documents", db.signed_document, "family_group_id"),
                     ("tasks", db.task, "family_group_id"),
                     ("document links", db.document_link, "family_group_id"),
                     ("entity roles", db.entity_role, "family_group_id"),
                     ("ongoing fee arrangements", db.ofa_arrangement, "family_group_id")],
    "entity": [("roles", db.entity_role, "entity_id"), ("accounts", db.account, "entity_id"),
               ("signed documents", db.signed_document, "entity_id"),
               ("document links", db.document_link, "entity_id")],
}


def _child_counts(conn, kind: str, rid: str) -> dict[str, int]:
    return {name: conn.execute(sa.select(sa.func.count()).select_from(t).where(
        getattr(t.c, col) == rid)).scalar_one() for name, t, col in CHILD_TABLES[kind]}


# -- compare ---------------------------------------------------------------------------------

def compare(engine: sa.Engine, kind: str, a_id: str, b_id: str) -> dict:
    if a_id == b_id:
        raise MergeError("Pick two different records.")
    with engine.connect() as conn:
        ra, rb = _row(conn, kind, a_id), _row(conn, kind, b_id)
        va, la = _values(conn, kind, a_id)
        vb, lb = _values(conn, kind, b_id)
        children = {"a": _child_counts(conn, kind, a_id), "b": _child_counts(conn, kind, b_id)}
    labels = {**lb, **la}
    order = list(dict.fromkeys(list(va) + list(vb)))
    fields = []
    for key in order:
        a, b = va.get(key), vb.get(key)
        if _blank(a) and _blank(b):
            continue
        if key.startswith("ffgroup:"):
            state = "same" if a == b else ("a_only" if _blank(b) else "b_only" if _blank(a)
                                           else "conflict")
        else:
            state = ("same" if _same(a, b) else "a_only" if _blank(b) else "b_only"
                     if _blank(a) else "conflict")
        fields.append({"key": key, "label": labels[key], "a": _plain(a), "b": _plain(b),
                       "state": state, "group": key.startswith("ffgroup:")})
    summary = {s: sum(f["state"] == s for f in fields) for s in ("same", "a_only", "b_only", "conflict")}
    return {"kind": kind,
            "a": {"id": a_id, "name": ra["name"], "xplan_id": ra.get("xplan_id"), "status": ra.get("status")},
            "b": {"id": b_id, "name": rb["name"], "xplan_id": rb.get("xplan_id"), "status": rb.get("status")},
            "fields": fields, "summary": summary, "children": children}


# -- apply -----------------------------------------------------------------------------------

def _resolve(key: str, keep_v, drop_v, keep: str, drop: str, picks: dict, exclude: set):
    """The merged value for one key, and what decided it."""
    if key in exclude:
        return keep_v, "excluded"
    pick = picks.get(key)
    if key.startswith("ffgroup:") and pick == "combine":
        out = list(keep_v or [])
        for item in drop_v or []:
            if item not in out:
                out.append(item)
        return out, "combined"
    if pick == drop:
        return drop_v, "picked dropped"
    if pick == keep:
        return keep_v, "picked kept"
    if _blank(keep_v) and not _blank(drop_v):
        return drop_v, "filled from dropped"
    return keep_v, "kept"


def apply_merge(engine: sa.Engine, kind: str, keep: str, drop: str, *,
                picks: dict[str, str] | None = None, exclude: set[str] | list[str] | None = None,
                actor: str = "") -> dict:
    """Merge `drop` into `keep`. picks: {key: record id whose value wins}; exclude: keys that
    keep the kept record's value. Returns what changed."""
    picks, exclude = dict(picks or {}), set(exclude or ())
    for k, v in picks.items():
        if v not in (keep, drop, "combine"):
            raise MergeError(f"pick for {k} must be {keep}, {drop} or combine")
    if keep == drop:
        raise MergeError("Pick two different records.")
    t = _table(kind)
    now = dt.datetime.now(dt.timezone.utc)
    report: dict[str, Any] = {"kept": keep, "dropped": drop, "fields": {}, "moved": {}}
    from . import audit
    with engine.begin() as conn:
        audit.set_actor(conn, actor or "merge tool")
        rk, rd = _row(conn, kind, keep), _row(conn, kind, drop)
        vk, _ = _values(conn, kind, keep)
        vd, _ = _values(conn, kind, drop)
        merged = {}
        for key in dict.fromkeys(list(vk) + list(vd)):
            merged[key], how = _resolve(key, vk.get(key), vd.get(key), keep, drop, picks, exclude)
            if how not in ("kept",) or key in exclude:
                report["fields"][key] = how

        # 1. the record's own columns
        cols = FG_FIELDS if kind == "family_group" else ENTITY_FIELDS
        upd = {c: merged.get(c) for c, _ in cols}
        if upd.get("home_family_group_id") == drop:
            upd["home_family_group_id"] = None
        if kind == "family_group":
            plain = {k[3:]: v for k, v in merged.items() if k.startswith("ff:") and not _blank(v)}
            groups = {k[8:]: v for k, v in merged.items() if k.startswith("ffgroup:") and v}
            upd["ff_answers"], upd["ff_counts"] = _join_ff(plain, groups)
        else:
            upd["details"] = {k[8:]: v for k, v in merged.items()
                              if k.startswith("details:") and not _blank(v)}
        upd["updated_at"] = now
        snapshot = {k: _plain(v) for k, v in rd.items()}

        # 2. people (family groups): merge details by position, move the ones only `drop` has
        if kind == "family_group":
            pk, pd = _people(conn, keep), _people(conn, drop)
            for pos in sorted(set(pk) | set(pd)):
                vals = {c: merged.get(f"person:{pos}:{c}") for c, _ in PERSON_FIELDS}
                new_id = f"{keep}:{pos}"
                if pos in pk:
                    conn.execute(db.person.update().where(db.person.c.id == new_id).values(**vals))
                else:
                    conn.execute(db.person.insert().values(id=new_id, family_group_id=keep,
                                                           position=pos, **vals))
                if pos in pd:
                    conn.execute(db.entity_role.update().where(
                        db.entity_role.c.person_id == pd[pos]["id"]).values(person_id=new_id))
            snapshot["people"] = [{k: _plain(v) for k, v in p.items()} for p in pd.values()]
            conn.execute(db.person.delete().where(db.person.c.family_group_id == drop))
            if len(set(pk) | set(pd)) == 2 and upd.get("type") == "Individual" and "type" not in exclude \
                    and picks.get("type") is None:
                upd["type"] = "Couple"
                report["fields"]["type"] = "set to Couple (two people after merge)"

        conn.execute(t.update().where(t.c.id == keep).values(**upd))

        # 3. move everything attached to `drop`
        if kind == "family_group":
            have = {(r.kind, (r.value or "").strip().casefold()) for r in conn.execute(
                sa.select(db.contact_point.c.kind, db.contact_point.c.value).where(
                    db.contact_point.c.family_group_id == keep))}
            for r in conn.execute(sa.select(db.contact_point).where(
                    db.contact_point.c.family_group_id == drop)).mappings().all():
                if (r["kind"], (r["value"] or "").strip().casefold()) in have:
                    conn.execute(db.contact_point.delete().where(db.contact_point.c.id == r["id"]))
                else:
                    conn.execute(db.contact_point.update().where(db.contact_point.c.id == r["id"])
                                 .values(family_group_id=keep, is_primary=False))
            taken = {r[0] for r in conn.execute(sa.select(db.account.c.id).where(
                db.account.c.family_group_id == keep))}
            moved_accounts = 0
            for r in conn.execute(sa.select(db.account.c.id, db.account.c.source_ref).where(
                    db.account.c.family_group_id == drop)).all():
                new_id, n = f"{keep}:{r.source_ref}", 2
                while new_id in taken:
                    new_id, n = f"{keep}:{r.source_ref}-{n}", n + 1
                taken.add(new_id)
                acct = dict(conn.execute(sa.select(db.account).where(db.account.c.id == r.id)).mappings().one())
                conn.execute(db.account.delete().where(db.account.c.id == r.id))
                conn.execute(db.account.insert().values(**{**acct, "id": new_id, "family_group_id": keep}))
                moved_accounts += 1
            report["moved"]["accounts"] = moved_accounts
            for name, tb, col in CHILD_TABLES["family_group"]:
                if tb in (db.person, db.contact_point, db.account):
                    continue
                n = conn.execute(tb.update().where(getattr(tb.c, col) == drop)
                                 .values({col: keep})).rowcount
                report["moved"][name] = n
            conn.execute(db.entity.update().where(db.entity.c.home_family_group_id == drop)
                         .values(home_family_group_id=keep))
            for old_pos in (1, 2):
                conn.execute(db.entity.update().where(db.entity.c.primary_ref == f"{drop}:{old_pos}")
                             .values(primary_ref=f"{keep}:{old_pos}"))
        else:
            for name, tb, col in CHILD_TABLES["entity"]:
                report["moved"][name] = conn.execute(tb.update().where(getattr(tb.c, col) == drop)
                                                     .values({col: keep})).rowcount

        _dedupe_roles(conn)

        # 4. drop the duplicate, remember the merge, log it on both ids
        conn.execute(t.delete().where(t.c.id == drop))
        choices = {"picks": picks, "exclude": sorted(exclude), "result": report["fields"]}
        conn.execute(db.merge_record.insert().values(
            kind=kind, kept_id=keep, dropped_id=drop, dropped_xplan_id=rd.get("xplan_id"),
            at=now, actor=actor or None, choices=json.loads(json.dumps(choices, default=str)),
            snapshot=json.loads(json.dumps(snapshot, default=str))))
        rtype = "family_group" if kind == "family_group" else "entity"
        conn.execute(db.change_log.insert(), [
            {"at": now, "actor": actor or None, "record_type": rtype, "record_id": keep,
             "kind": "Merged", "message": f"Merged {drop} into this record"},
            {"at": now, "actor": actor or None, "record_type": rtype, "record_id": drop,
             "kind": "Merged", "message": f"Merged into {keep}; this record no longer exists"}])
    return report


def _dedupe_roles(conn) -> None:
    """After a merge the same person can hold two role rows on one entity: fold them into one
    (roles combined, the larger balance kept, primary if either was)."""
    rows = conn.execute(sa.select(db.entity_role)).mappings().all()
    seen: dict[tuple, dict] = {}
    for r in rows:
        k = (r["entity_id"], r["family_group_id"], r["person_pos"])
        if k not in seen:
            seen[k] = dict(r)
            continue
        first = seen[k]
        roles = list(dict.fromkeys(list(first["roles"] or []) + list(r["roles"] or [])))
        bal = max([b for b in (first["member_balance"], r["member_balance"]) if b is not None],
                  default=None)
        first.update(roles=roles, member_balance=bal,
                     is_primary=bool(first["is_primary"] or r["is_primary"]))
        conn.execute(db.entity_role.update().where(db.entity_role.c.id == first["id"]).values(
            roles=roles, member_balance=bal, is_primary=first["is_primary"]))
        conn.execute(db.entity_role.delete().where(db.entity_role.c.id == r["id"]))


# -- finding duplicates ----------------------------------------------------------------------

def _norm_name(s: str | None) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("&", " ")).split()
    noise = {"and", "the", "mr", "mrs", "ms", "dr", "pty", "ltd", "atf", "trust", "trustee",
             "for", "super", "fund", "superannuation", "smsf"}
    return " ".join(sorted(w for w in words if w not in noise))


def find_candidates(engine: sa.Engine, kind: str, limit: int = 500) -> list[dict]:
    """Pairs that look like the same family group or entity, strongest first."""
    keys: dict[tuple, set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    with engine.connect() as conn:
        if kind == "family_group":
            for r in conn.execute(sa.select(db.family_group.c.id, db.family_group.c.name,
                                            db.family_group.c.email, db.family_group.c.phone)):
                names[r.id] = r.name
                if _norm_name(r.name):
                    keys[("same name", _norm_name(r.name))].add(r.id)
            for r in conn.execute(sa.select(db.contact_point.c.family_group_id,
                                            db.contact_point.c.kind, db.contact_point.c.value)):
                v = (r.value or "").strip().lower()
                if r.kind == "phone":
                    v = re.sub(r"\D", "", v)[-9:]
                if len(v) >= 6:
                    keys[(f"same {r.kind}", v)].add(r.family_group_id)
            for r in conn.execute(sa.select(db.person.c.family_group_id, db.person.c.name,
                                            db.person.c.dob)):
                if r.dob and r.name:
                    last = (r.name.split() or [""])[-1].lower()
                    keys[("same person (surname + date of birth)", f"{last}|{r.dob}")].add(
                        r.family_group_id)
        else:
            for r in conn.execute(sa.select(db.entity.c.id, db.entity.c.name, db.entity.c.abn)):
                names[r.id] = r.name
                if _norm_name(r.name):
                    keys[("same name", _norm_name(r.name))].add(r.id)
                abn = re.sub(r"\D", "", r.abn or "")
                if len(abn) == 11:
                    keys[("same ABN", abn)].add(r.id)
        merged_away = {r[0] for r in conn.execute(sa.select(db.merge_record.c.dropped_id))}
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (reason, _), ids in keys.items():
        ids = sorted(i for i in ids if i in names and i not in merged_away)
        if 2 <= len(ids) <= 6:   # a key shared by many records (a shared office phone) says little
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    pairs[(a, b)].add(reason)
    out = [{"a": a, "b": b, "a_name": names[a], "b_name": names[b], "reasons": sorted(rs)}
           for (a, b), rs in pairs.items()]
    out.sort(key=lambda p: (-len(p["reasons"]), p["a_name"] or ""))
    return out[:limit]
