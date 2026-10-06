"""Mark households ACTIVE from the "fees by client" revenue report (xlsx).

The report has one block per client: a "Clients: <name>" heading row, the client's fee
transactions (Policy Number, Client No, Client / Owner, CRM Reference ...) and a total row.
Each block is matched to an Xplan client, strongest evidence first:

1. CRM Reference  - the Xplan entity id
2. Policy Number  - matched to the policy numbers on Xplan's fee (FDS) records
3. Name           - only when it matches exactly one Xplan client
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

HEADINGS = {"policy number": "policy", "client no": "client_no", "client / owner": "owner",
            "crm reference": "crm", "transaction no": "txn"}


@dataclass
class ListedClient:
    name: str
    policies: set[str] = field(default_factory=set)
    owners: set[str] = field(default_factory=set)
    client_nos: set[str] = field(default_factory=set)
    crm_refs: set[int] = field(default_factory=set)
    households: set[str] = field(default_factory=set)
    method: str = ""


def _norm_policy(value) -> str:
    return re.sub(r"[\s\-/]", "", str(value or "")).upper().lstrip("0")


def norm_name(value) -> str:
    """Comparable name: lower case, "Last, First" -> "first last", no titles/punctuation."""
    text = str(value or "").strip()
    if "," in text:
        last, _, first = text.partition(",")
        text = f"{first} {last}"
    text = re.sub(r"\b(mr|mrs|ms|miss|dr|prof)\b\.?", " ", text, flags=re.I)
    words = re.findall(r"[a-z0-9']+", text.lower())
    return " ".join(sorted(words))


def read_active_list(path: Path) -> list[ListedClient]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    clients: list[ListedClient] = []
    for ws in wb.worksheets:
        cols: dict[str, int] = {}
        current: ListedClient | None = None
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() if c is not None else "" for c in row]
            if not cols:
                found = {HEADINGS[re.sub(r"\s+", " ", c).lower()]: i
                         for i, c in enumerate(cells)
                         if re.sub(r"\s+", " ", c).lower() in HEADINGS}
                if "policy" in found and "owner" in found:
                    cols = found
                continue
            first = next((c for c in cells if c), "")
            if first.lower().startswith("clients:"):
                current = ListedClient(first.split(":", 1)[1].strip())
                clients.append(current)
                continue
            if current is None or first.lower().startswith(("total", "grand total")):
                continue
            get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""
            if not re.fullmatch(r"\d+", get("txn") or first):
                continue
            if get("policy"):
                current.policies.add(_norm_policy(get("policy")))
            if get("owner"):
                current.owners.add(get("owner"))
            if get("client_no"):
                current.client_nos.add(get("client_no"))
            if re.fullmatch(r"\d{1,9}", get("crm")):
                current.crm_refs.add(int(get("crm")))
    return clients


def match_active(listed: list[ListedClient], builder, entity_home: dict[int, str]) -> set[str]:
    """Fill in each listed client's households; return all active household ids."""
    d = builder.d
    home_of = lambda eid: builder.person_home.get(eid, (entity_home.get(eid),))[0]

    by_policy: dict[str, set[int]] = {}
    for eid, rows in d.fds.items():
        for r in rows:
            p = _norm_policy(r.get("policy_number"))
            if p:
                by_policy.setdefault(p, set()).add(eid)

    by_name: dict[str, set[str]] = {}
    for hid, hh in builder.households.items():
        for p in hh.people:
            for n in (p.name, p.f.get("entity_name")):
                if n:
                    by_name.setdefault(norm_name(n), set()).add(hid)
    for eid, hid in entity_home.items():
        e = d.entities.get(eid)
        if e:
            by_name.setdefault(norm_name(e.name), set()).add(hid)

    active: set[str] = set()
    for c in listed:
        homes = {home_of(e) for e in c.crm_refs} - {None}
        if homes:
            c.method = "CRM Reference"
        else:
            homes = {home_of(e) for p in c.policies for e in by_policy.get(p, ())} - {None}
            if homes:
                c.method = "Policy Number"
        if not homes:
            for n in [c.name, *c.owners]:
                found = by_name.get(norm_name(n), set())
                if len(found) == 1:
                    homes, c.method = set(found), "Name (unique match)"
                    break
        c.households = homes
        active |= homes
    return active


def write_report(listed: list[ListedClient], path: Path) -> tuple[int, int]:
    matched = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["listed_client", "matched", "how", "brightly_household_ids", "policies",
                    "crm_references"])
        for c in listed:
            matched += bool(c.households)
            w.writerow([c.name, "yes" if c.households else "NO", c.method,
                        "; ".join(sorted(c.households)), len(c.policies),
                        "; ".join(str(x) for x in sorted(c.crm_refs))])
    return matched, len(listed)


def active_entity_ids(builder, entities: list[dict], households: set[str]) -> set[int]:
    """Everyone and everything belonging to an active household."""
    ids = {p.id for hid in households for p in builder.households[hid].people}
    for e in entities:
        if e["home"] in households or any(r["c"] in households for r in e["roles"]):
            ids.add(int(e["ext"]["xplan"]))
    return ids


def compute_active(engine, list_path: Path, progress=print):
    """For the documents export: (active entity ids, listed clients) from the fee report."""
    from .xplan_map import BrightlyBuilder, XplanData, XplanDb

    raw = engine.raw_connection()
    conn = raw.driver_connection if hasattr(raw, "driver_connection") else raw.connection
    try:
        data = XplanData(XplanDb(conn), progress)
        builder = BrightlyBuilder(data, None)
        entities = builder.entity_records()
        entity_home = {int(e["ext"]["xplan"]): e["home"] for e in entities}
        listed = read_active_list(list_path)
        households = match_active(listed, builder, entity_home)
        return active_entity_ids(builder, entities, households), listed
    finally:
        raw.close()
