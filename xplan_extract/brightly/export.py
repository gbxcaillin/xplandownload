"""Run the Xplan -> Brightly export and write the deliverable folder."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Callable

import sqlalchemy as sa

from . import ExportWriter, Schema, Unmapped
from .xplan_map import (BrightlyBuilder, XplanData, XplanDb, build_notes, build_tasks, qname,
                        tfn_like_sql, unmapped_inventory)

Progress = Callable[[str], None]
TOOL_DIR = Path(__file__).resolve().parents[2]


class UnsafeDestination(Exception):
    pass


def check_destination(out: Path, allowed_root: Path | None, allow_any: bool) -> Path:
    """Client data may only go to the secure SharePoint area (BRIGHTLY_OUT)."""
    out = out.resolve()
    if allow_any:
        return out
    if TOOL_DIR == out or TOOL_DIR in out.parents:
        raise UnsafeDestination(
            f"Refusing to write client data inside the tool folder ({TOOL_DIR}). Use the "
            "secure SharePoint folder (BRIGHTLY_OUT in .env).")
    if allowed_root is not None:
        root = allowed_root.resolve()
        if out != root and root not in out.parents:
            raise UnsafeDestination(
                f"Refusing to write outside the secure export folder {root}. "
                "Pass --allow-any-destination only if you are sure.")
    return out


def choose_sample(builder: BrightlyBuilder, entities: list[dict], size: int) -> list[str]:
    """A couple, an SMSF household, a trust household, then the fullest records."""
    hh = builder.households
    picked: list[str] = []

    def take(hid):
        if hid and hid not in picked and len(picked) < size:
            picked.append(hid)

    for kind in ("SMSF", "Family trust", "Unit trust", "Company"):
        for e in entities:
            if e["type"] == kind and e["home"] not in picked:
                take(e["home"])
                break
    couples = sorted((h for h in hh.values() if len(h.people) == 2),
                     key=lambda h: -len(h.accounts))
    if couples and not any(len(hh[p].people) == 2 for p in picked):
        take(couples[0].id)
    for h in sorted(hh.values(), key=lambda h: (-len(h.accounts), h.id)):
        take(h.id)
    return picked


def export_brightly(engine: sa.Engine, out_dir: Path, schema_path: str | None, source: str,
                    sample: int = 0, progress: Progress = print,
                    active_list: Path | None = None, skip_unmapped: bool = False) -> dict:
    schema = Schema(schema_path)
    raw = engine.raw_connection()
    conn = raw.driver_connection if hasattr(raw, "driver_connection") else raw.connection
    try:
        db = XplanDb(conn)
        data = XplanData(db, progress)
        builder = BrightlyBuilder(data, schema)
        progress(f"Households: {len(builder.households):,} "
                 f"({sum(len(h.people) == 2 for h in builder.households.values()):,} couples)")

        for h in builder.households.values():
            builder.add_platform_accounts(h, [p.id for p in h.people])
        entities = builder.entity_records()
        progress(f"SMSFs, trusts and companies with household roles: {len(entities):,}")
        entity_home = {int(e["ext"]["xplan"]): e["home"] for e in entities}
        listed = None
        if active_list:
            from .active import match_active, read_active_list
            listed = read_active_list(active_list)
            builder.active = match_active(listed, builder, entity_home)
            progress(f"Active-clients list: {len(listed)} client(s) -> "
                     f"{len(builder.active)} active household(s)")

        wanted = (choose_sample(builder, entities, sample) if sample
                  else sorted(builder.households))
        wanted_set = set(wanted)
        if sample:
            progress(f"SAMPLE: {len(wanted)} household(s)")

        progress("File notes, advice history and signed documents ...")
        notes, advice, signed = build_notes(data, builder, wanted_set, entity_home, progress)
        tasks = build_tasks(data, builder, wanted_set, entity_home)

        writer = ExportWriter(out_dir, source, schema.version)
        for hid in wanted:
            h = builder.households[hid]
            record = builder.household_record(h, notes.get(hid, []), advice.get(hid, []),
                                              signed.get(hid, {}))
            writer.write("prospects" if record.get("prospect") else "households", record)
        for e in entities:
            if builder.active is not None:
                e["status"] = "Active" if (e["home"] in builder.active or any(
                    r["c"] in builder.active for r in e["roles"])) else "Inactive"
            if e["home"] in wanted_set or any(r["c"] in wanted_set for r in e["roles"]):
                if sample:  # keep only the sampled households' links
                    e = {**e, "roles": [r for r in e["roles"] if r["c"] in wanted_set],
                         "accts": [a for a in e["accts"] if a["c"] in wanted_set]}
                writer.write("entities", e)
        for t in tasks:
            if builder.active is not None:
                t["status"] = builder.status(t["c"])
            writer.write("tasks", t)

        if skip_unmapped:
            progress("Skipping the unmapped-field scan (--skip-unmapped).")
        else:
            progress("Listing Xplan fields that aren't mapped (a few minutes) ...")
            writer.add_unmapped(unmapped_inventory(data, progress))
        writer.add_unmapped(builder.problems)
        writer.sensitive_values = builder.sensitive
        cur = conn.cursor()
        for t, col in data.tfn_columns:
            cur.execute(f"SELECT COUNT(*) FROM {qname(t)} WHERE {tfn_like_sql(col)}")
            writer.tfn_fields_dropped += int(cur.fetchone()[0])
        if listed is not None:
            from .active import write_report
            matched, total = write_report(listed, out_dir / "active_match.csv")
            writer.notes.append(
                f"- **Active / Inactive**: {matched} of {total} clients on the fee list were "
                f"matched to a household (`active_match.csv` shows how; unmatched ones say NO). "
                f"{len(builder.active):,} households are \"Active\", the rest \"Inactive\" "
                f"(prospects included). Households, prospects, entities and tasks carry "
                f"`\"status\": \"Active\" | \"Inactive\"` - a field the brief doesn't define "
                f"yet. **Brightly change needed:** hide `status = \"Inactive\"` records by "
                f"default (lists, search results, portal); show them when the user filters on, "
                f"or searches for, \"inactive\".")
        manifest = writer.close(_readme(builder, writer, sample, wanted))
        manifest["sample"] = bool(sample)
        return manifest
    finally:
        raw.close()


def _readme(builder: BrightlyBuilder, writer: ExportWriter, sample: int, wanted: list[str]) -> str:
    c = builder.counts
    problems = Counter((u.field, u.suggested_target, u.reason) for u in builder.problems)
    value_issues = "\n".join(f"- `{f}` → {t}: {r} ({n}×)" for (f, t, r), n in
                             problems.most_common(40)) or "- none"
    return f"""# Xplan → Brightly export

Exported {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}{' — **SAMPLE of ' + str(len(wanted)) + ' households for checking**' if sample else ''}.
Record counts, TFN and sensitive-field counts are in `manifest.json`.

## How Xplan maps to Brightly

| Brightly | Xplan source |
|---|---|
| household (client = person 1, partner = person 2) | `entity_clients` (individuals); couples from `clientrelation_marrying` (subject = client) and `partner_entity_id` |
| household id / `ext.xplan` | `H-<Xplan entity id of the client>`; ext.xplan = that id, so a re-run updates rather than duplicates |
| name, people (name, DOB, job, income) | entity first/last name, `dob`; job and income from the primary `ufield_entity_employment` row (`ordinary_wages`) |
| adviser | `client_adviser` ("Last, First" → "First Last") |
| since | `client_active_date`, else the record's `create_date` |
| email / phone / contacts | `ufield_entity_contact` (preferred first), then `preferred_email` / `preferred_phone` |
| city, ff address | preferred residential `ufield_entity_address` |
| lastReview / nextReview | `ufield_entity_review`: latest completed / earliest not completed or cancelled (may be overdue) |
| ofa | `disclosure_statement_date` (Xplan "Next Disclosure Statement Date") |
| insRenewal | earliest future `renewal_date` on the clients' insurance covers |
| fee / feeType | fixed ongoing fees in `ufield_entity_NAG_fees_cust`; otherwise ongoing fees received in the last 12 months (`ufield_entity_banyan_fds_cust`) → feeType "Ongoing" |
| accounts | super funds (`ufield_entity_fund`, not cancelled), pensions (`ufield_entity_retirement_income`), platform accounts (`ips_subfund`, balance = sum of current `ips_position` values) |
| assets / liabs | `entity_assets` / `entity_liabilities` on the client's record (owner Client/Partner/Joint → 1/2/joint); "Recommended" assets left out |
| goals | client objectives, goals and objectives lists (descriptions) |
| risk | joint risk profile for couples, otherwise the client's; only exact Brightly options are used |
| fileNotes | `sections_workflow_docnote` linked to either person (text converted from HTML) |
| adviceHist | file notes whose type/sub-type is Statement of Advice / Record of Advice |
| signed.atp | latest "Authority to Proceed" file note |
| entities | SMSFs, trusts and companies with roles from the trustee, director, fund member, shareholder, beneficiary and settlor lists; a corporate trustee's directors get the Director role |
| tasks | `sections_workflow_task` (Complete → done; Aborted tasks left out) |
| prospects | households whose Xplan client status is Prospect |

## Decisions to confirm (Scott)

- **Ambiguous values not converted** (also in `unmapped.csv`):
{value_issues}
- Trust type: {c.get('trust_type_assumed_family', 0)} trust(s) had no clear type and were exported as "Family trust".
- SMSF trustee type text used: "Individual trustees" / "Corporate trustee" — check these match Brightly's options.
- fee taken from last-12-month FDS receipts for {c.get('fee_from_fds', 0)} household(s).
- Roles for people who aren't in any household were skipped ({c.get('roles_for_people_without_household', 0)}); structures with no household role skipped ({c.get('entities_without_household_roles', 0)}).
- {c.get('entities_linked_by_relationship', 0)} structure(s) had no trustee/director/member list and were linked through Xplan's general relationships instead (Trustee, Director, Super → Member ...); {c.get('relationship_role_unclear', 0)} link(s) had a label with no clear role (e.g. "Company", "Trust", "Family") and are shown as role "Associated".
- TFN removal in text uses the ATO check digit; an unrelated 8–9 digit number (e.g. some account numbers) can pass it by chance and is also removed (Brightly would refuse it anyway).
- Aborted tasks left out: {c.get('tasks_aborted_skipped', 0)}.
- Signed documents other than ATP (OFA consent, fee consent "Consent Form" notes, ID checks in `sections_identitycheck`) need Brightly's doc keys before they can be mapped.
- File note text comes across as written in Xplan. Some notes may mention health details; they have not been altered.
- Assets entered manually in Xplan (e.g. "Platforms") may overlap with platform accounts from data feeds.
{chr(10).join(writer.notes)}
- Documents themselves are in SharePoint (`XPlan Files\\Clients\\...`), listed in `documents_index.csv`.

## Rules applied

- No tax file numbers: TFN columns are never read; only whether one is held. Any number in any value that
  passes the ATO TFN check digit is replaced with "[TFN removed]".
- Health answers only go into the fact find's health fields (e.g. `smoker12m`).
- Dates are YYYY-MM-DD (file notes: ISO date-time, Melbourne time), money is plain numbers, UTF-8.
- `unmapped.csv`: every Xplan field filled in for at least one record that isn't mapped above, with the
  number of records that have it and a suggested Brightly target.
"""
