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

from . import clean_text

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


def _cell(value) -> str:
    """Excel stores 12345 as 12345.0 - give the number back as written."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip() if value is not None else ""


def _norm_policy(value) -> str:
    return re.sub(r"[\s\-/]", "", str(value or "")).upper().lstrip("0")


NOISE_WORDS = {"mr", "mrs", "ms", "miss", "dr", "prof", "the", "pty", "ltd", "limited", "p", "l",
               "superannuation", "super", "fund", "smsf", "trust", "trustee", "trustees",
               "as", "for", "atf", "a", "t", "f", "family", "investments", "investment"}


def norm_name(value) -> str:
    """Comparable name: lower case, "Last, First" -> "first last", no titles/punctuation."""
    text = str(value or "").strip()
    if "," in text:
        last, _, first = text.partition(",")
        text = f"{first} {last}"
    text = re.sub(r"\b(mr|mrs|ms|miss|dr|prof)\b\.?", " ", text, flags=re.I)
    words = re.findall(r"[a-z0-9']+", text.lower())
    return " ".join(sorted(words))


def loose_name(value) -> str:
    """Like norm_name but also ignoring Pty Ltd, Super Fund, ATF, Trust ... wording."""
    return " ".join(w for w in norm_name(value).split() if w not in NOISE_WORDS)


def name_variants(value) -> list[str]:
    """A fee-report name and the people/structures it may stand for.

    "Citizen, Jane & Sam" -> "Citizen, Jane & Sam", "Jane Citizen", "Sam Citizen";
    "Smith Pty Ltd ATF Smith Family Trust" -> also each side of ATF.
    """
    text = str(value or "").strip()
    out = [text]
    for part in re.split(r"\s+(?:atf|a/t/f|as trustee for|itf)\s+", text, flags=re.I):
        if part and part != text:
            out.append(part)
    if "," in text:
        last, _, firsts = text.partition(",")
        for first in re.split(r"\s*(?:&|\band\b|/)\s*", firsts):
            if first.strip():
                out.append(f"{first.strip()} {last.strip()}")
    else:
        m = re.match(r"^(.+?)\s*(?:&|\band\b)\s*(.+?)\s+(\S+)$", text)
        if m:  # "Jane & Sam Citizen"
            out += [f"{m.group(1)} {m.group(3)}", f"{m.group(2)} {m.group(3)}"]
    return out


def read_active_list(path: Path) -> list[ListedClient]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    clients: list[ListedClient] = []
    for ws in wb.worksheets:
        cols: dict[str, int] = {}
        current: ListedClient | None = None
        for row in ws.iter_rows(values_only=True):
            cells = [_cell(c) for c in row]
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
    by_loose: dict[str, set[str]] = {}

    def index(name, hid, structure=False):
        if name:
            by_name.setdefault(norm_name(name), set()).add(hid)
            # loose matching (ignoring Pty Ltd / ATF / Fund wording) only for structures
            if structure and loose_name(name):
                by_loose.setdefault(loose_name(name), set()).add(hid)

    for hid, hh in builder.households.items():
        for p in hh.people:
            index(p.name, hid)
            index(p.f.get("entity_name"), hid)
            pref = clean_text(p.f.get("preferred_name"))
            if pref and clean_text(p.f.get("last_name")):
                index(f"{pref} {clean_text(p.f.get('last_name'))}", hid)
    for eid, hid in entity_home.items():
        e = d.entities.get(eid)
        if e:
            index(e.name, hid, structure=True)
            index(e.f.get("entity_name"), hid, structure=True)

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
            names = [v for n in [c.name, *c.owners] for v in name_variants(n)]
            for lookup, label in ((by_name, "Name (unique match)"),
                                  (by_loose, "Name, ignoring Pty Ltd/ATF/Fund wording")):
                found: set[str] = set()
                for n in names:
                    key = norm_name(n) if lookup is by_name else loose_name(n)
                    hit = lookup.get(key, set())
                    if len(hit) == 1:
                        found |= hit
                if len(found) == 1:
                    homes, c.method = found, label
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


def _shape(value: str) -> str:
    text = re.sub(r"[A-Z]", "A", str(value))
    text = re.sub(r"[a-z]", "a", text)
    return re.sub(r"\d", "9", text)


def diagnose(listed: list[ListedClient], builder, entity_home: dict[int, str]) -> list[str]:
    """Why listed clients didn't match - counts and masked shapes only, no names."""
    from collections import Counter

    d = builder.d
    unmatched = [c for c in listed if not c.households]
    lines = [f"Unmatched: {len(unmatched)} of {len(listed)}"]
    reasons: Counter = Counter()
    surnames = Counter()
    for hh in builder.households.values():
        for p in hh.people:
            last = clean_text(p.f.get("last_name"))
            if last:
                surnames[last.lower()] += 1
    for c in unmatched:
        if c.crm_refs:
            for ref in c.crm_refs:
                e = d.entities.get(ref)
                if e is None:
                    reasons["CRM Reference not an Xplan id in this extract"] += 1
                elif ref not in builder.person_home and ref not in entity_home:
                    kind = e.type or "unknown type"
                    in_clients = "in" if ref in d.clients else "not in"
                    reasons[f"CRM Reference is an Xplan {kind} {in_clients} the client list, "
                            f"not part of a household"] += 1
            continue
        name = c.name
        words = re.findall(r"[A-Za-z']+", name)
        if re.search(r"pty|ltd|limited|trust|super|fund|smsf|atf|holdings|investments|"
                     r"partnership|estate", name, re.I):
            reasons["company / trust / fund style name"] += 1
        elif "&" in name or re.search(r"\band\b", name, re.I):
            reasons["two people on one line"] += 1
        elif any(len(w) == 1 for w in words):
            reasons["contains initials"] += 1
        else:
            last = (name.split(",")[0] if "," in name else (words[-1] if words else "")).strip()
            if surnames.get(last.lower()):
                reasons["surname exists in Xplan, but no exact first-name match"] += 1
            else:
                reasons["surname not found in any Xplan household"] += 1
    lines += [f"  {n:>4}  {r}" for r, n in reasons.most_common()]
    report = Counter(_shape(p) for c in unmatched for p in c.policies)
    xplan = Counter(_shape(_norm_policy(r.get("policy_number")))
                    for rows in d.fds.values() for r in rows if r.get("policy_number"))
    lines.append("Policy number formats on the report (unmatched clients): "
                 + ", ".join(f"{s} ({n})" for s, n in report.most_common(6)))
    lines.append("Policy number formats in Xplan's fee records:            "
                 + ", ".join(f"{s} ({n})" for s, n in xplan.most_common(6)))
    lines.append(f"Xplan fee (FDS) records cover {len(d.fds):,} client records.")
    return lines
