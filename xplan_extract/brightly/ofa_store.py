"""Ongoing fee arrangements in the Brightly database: create them from the migrated records,
record consents and the notices the rules require, and list what needs doing.

Every step writes an `ofa_event` (append-only), so the 5-year evidence trail builds itself.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

import sqlalchemy as sa

from . import database as db
from . import ofa

CONSENT_HISTORY = "Confirm the last consent (Xplan history not imported)"
NOTE_PATTERN = re.compile(r"consent|renewal|\bofa\b|ongoing fee|fee disclosure|\bfds\b", re.I)


class OfaError(ValueError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _event(c, arrangement_id: str, kind: str, actor: str, detail: str = "",
           anniversary: dt.date | None = None, due_on: dt.date | None = None) -> None:
    c.execute(db.ofa_event.insert().values(
        arrangement_id=arrangement_id, at=_now(), kind=kind, actor=actor, detail=detail[:2000],
        anniversary=anniversary, due_on=due_on))


def _log(c, family_group_id: str, actor: str, message: str) -> None:
    c.execute(db.change_log.insert().values(at=_now(), actor=actor, record_type="family_group",
                                            record_id=family_group_id, kind="ofa",
                                            message=message))


# ----------------------------------------------------------------------------
# Creating arrangements from the migrated family groups
# ----------------------------------------------------------------------------

def suggested_last_consent(c, family_group_id: str, since: dt.date) -> tuple[dt.date, str] | None:
    """The latest Xplan file note that looks like a fee consent or renewal."""
    rows = c.execute(sa.select(db.file_note.c.at, db.file_note.c.title)
                     .where(db.file_note.c.family_group_id == family_group_id)
                     .order_by(db.file_note.c.at.desc())).all()
    for at, title in rows:
        day = at.date() if isinstance(at, dt.datetime) else at
        if day and day >= since and NOTE_PATTERN.search(title or ""):
            return day, title
    return None


def seed(engine: sa.Engine, actor: str, today: dt.date | None = None,
         only_active: bool = True, dry_run: bool = False) -> dict:
    """One arrangement per family group with an ongoing fee and an anniversary in Xplan
    (Xplan's "next disclosure statement date" carried the OFA anniversary). Family groups that
    already have an arrangement are skipped, so it is safe to run again."""
    today = today or dt.date.today()
    fg = db.family_group
    out = {"created": 0, "skipped_existing": 0, "no_anniversary": 0, "with_note_hint": 0,
           "rows": []}
    with engine.begin() as c:
        have = {r[0] for r in c.execute(sa.select(db.ofa_arrangement.c.family_group_id))}
        q = sa.select(fg).where(fg.c.is_prospect == sa.false())
        if only_active:
            q = q.where(fg.c.status == "Active")
        for r in c.execute(q).mappings():
            ongoing = (r["fee_type"] or "").lower() == "ongoing" or r["fee"]
            if not ongoing:
                continue
            if r["id"] in have:
                out["skipped_existing"] += 1
                continue
            if not r["ofa"]:
                out["no_anniversary"] += 1
                out["rows"].append((r["id"], r["name"], "no OFA anniversary in Xplan"))
                continue
            reference = ofa.anniversary(r["ofa"], 2024)   # so 2025 is the first new-rules year
            hint = suggested_last_consent(c, r["id"], today - dt.timedelta(days=730))
            review = CONSENT_HISTORY
            if hint:
                review += f"; Xplan note '{hint[1][:80]}' on {hint[0]:%d %b %Y}"
                out["with_note_hint"] += 1
            arrangement_id = f"OFA-{r['id']}-1"
            out["rows"].append((r["id"], r["name"], f"anniversary {reference:%d %b}"))
            out["created"] += 1
            if dry_run:
                continue
            c.execute(db.ofa_arrangement.insert().values(
                id=arrangement_id, family_group_id=r["id"], status="active",
                reference_day=reference, started_on=r["since"], fee_annual=r["fee"],
                fee_basis="Fixed annual fee" if r["fee"] else None, adviser=r["adviser"],
                needs_review=review, source="xplan", created_at=_now(), updated_at=_now()))
            _event(c, arrangement_id, "note", actor, "Created from the Xplan record. " + review)
    return out


# ----------------------------------------------------------------------------
# Day-to-day actions
# ----------------------------------------------------------------------------

def _arrangement(c, arrangement_id: str) -> dict:
    row = c.execute(sa.select(db.ofa_arrangement)
                    .where(db.ofa_arrangement.c.id == arrangement_id)).mappings().first()
    if not row:
        raise OfaError(f"No arrangement {arrangement_id}")
    return dict(row)


def add_account(engine: sa.Engine, arrangement_id: str, provider: str, actor: str, *,
                account_name: str = "", account_number: str = "", amount_annual=None,
                holders: str = "", account_id: str | None = None) -> None:
    with engine.begin() as c:
        a = _arrangement(c, arrangement_id)
        c.execute(db.ofa_account.insert().values(
            arrangement_id=arrangement_id, provider=provider, account_name=account_name or None,
            account_number=account_number or None, amount_annual=amount_annual,
            holders=holders or None, account_id=account_id))
        _event(c, arrangement_id, "note", actor, f"Deduction account added: {provider}")
        _log(c, a["family_group_id"], actor, f"Fee deduction account added ({provider})")


def record_consent(engine: sa.Engine, arrangement_id: str, signed_on: dt.date, method: str,
                   actor: str, *, signers: str = "", document_ref: str = "",
                   covers_deduction: bool = True, source: str = "brightly") -> dt.date:
    """Record a signed consent; returns the anniversary it renews. Refuses a signature outside
    the window (it would not be a valid renewal) and verbal consent."""
    if method not in ("esign", "paper", "electronic"):
        raise OfaError("Consent must be written: esign, paper or electronic (not verbal)")
    with engine.begin() as c:
        a = _arrangement(c, arrangement_id)
        if a["status"] != "active":
            raise OfaError(f"This arrangement is {a['status']}; a new arrangement is needed")
        anniv = ofa.consent_anniversary(a["reference_day"], signed_on)
        if anniv is None:
            raise OfaError(f"{signed_on:%d %b %Y} is outside every consent window "
                           f"(60 days before to 150 days after the anniversary)")
        exists = c.execute(sa.select(db.ofa_consent.c.id).where(
            db.ofa_consent.c.arrangement_id == arrangement_id,
            db.ofa_consent.c.anniversary == anniv)).first()
        if exists:
            raise OfaError(f"A consent is already recorded for the {anniv:%d %b %Y} anniversary")
        c.execute(db.ofa_consent.insert().values(
            arrangement_id=arrangement_id, anniversary=anniv, signed_on=signed_on,
            method=method, signers=signers or None, covers_deduction=covers_deduction,
            document_ref=document_ref or None, recorded_by=actor, recorded_at=_now(),
            source=source))
        review = (a["needs_review"] or "").split("; Xplan note")[0]
        if review == CONSENT_HISTORY:
            c.execute(db.ofa_arrangement.update().where(db.ofa_arrangement.c.id == arrangement_id)
                      .values(needs_review=None, updated_at=_now()))
        _event(c, arrangement_id, "consent_recorded", actor,
               f"Signed {signed_on:%d %b %Y} ({method}); keep until "
               f"{ofa.keep_until(signed_on):%d %b %Y}" + (f"; {document_ref}" if document_ref else ""),
               anniversary=anniv)
        _log(c, a["family_group_id"], actor,
             f"Ongoing fee consent recorded for the {anniv:%d %b %Y} anniversary")
    return anniv


def confirm_history(engine: sa.Engine, arrangement_id: str, actor: str,
                    last_signed: dt.date | None) -> None:
    """Staff confirm the last consent given under Xplan (or that there was none)."""
    if last_signed:
        record_consent(engine, arrangement_id, last_signed, "paper", actor, source="xplan",
                       document_ref="Consent given before Brightly (Xplan)")
        return
    with engine.begin() as c:
        _arrangement(c, arrangement_id)
        c.execute(db.ofa_arrangement.update().where(db.ofa_arrangement.c.id == arrangement_id)
                  .values(needs_review=None, updated_at=_now()))
        _event(c, arrangement_id, "note", actor, "Confirmed: no consent recorded before Brightly")


def mark_lapsed(engine: sa.Engine, arrangement_id: str, actor: str, today: dt.date,
                providers_notified_on: dt.date | None = None) -> None:
    """End an arrangement whose window closed without consent; optionally record that the
    account providers have been told (s962V)."""
    with engine.begin() as c:
        a = _arrangement(c, arrangement_id)
        st = _status_of(c, a, today)
        if st.state not in ("lapsed",) and a["status"] != "lapsed":
            raise OfaError(f"Not lapsed: {st.label}")
        if a["status"] != "lapsed":
            c.execute(db.ofa_arrangement.update().where(db.ofa_arrangement.c.id == arrangement_id)
                      .values(status="lapsed", ended_on=st.closes,
                              end_reason="No consent in the renewal window", updated_at=_now()))
            _event(c, arrangement_id, "lapsed", actor,
                   f"No consent by {st.closes:%d %b %Y}", anniversary=st.anniversary,
                   due_on=st.due_on)
            _log(c, a["family_group_id"], actor, "Ongoing fee arrangement lapsed (no consent)")
        if providers_notified_on:
            _event(c, arrangement_id, "providers_notified", actor,
                   f"Account providers told on {providers_notified_on:%d %b %Y}; fees stopped")


def record_withdrawal(engine: sa.Engine, arrangement_id: str, received_on: dt.date,
                      actor: str, detail: str = "") -> dict[str, dt.date]:
    """The client ended the arrangement in writing. Returns the 10-business-day deadlines."""
    due = ofa.withdrawal_deadlines(received_on)
    with engine.begin() as c:
        a = _arrangement(c, arrangement_id)
        c.execute(db.ofa_arrangement.update().where(db.ofa_arrangement.c.id == arrangement_id)
                  .values(status="ended", ended_on=received_on,
                          end_reason="Client withdrew consent", updated_at=_now()))
        _event(c, arrangement_id, "withdrawal_received", actor,
               f"Acknowledge, refund any later fees and tell providers by "
               f"{due['acknowledge_by']:%d %b %Y}. {detail}".strip(),
               due_on=due["acknowledge_by"])
        _log(c, a["family_group_id"], actor, "Client ended the ongoing fee arrangement")
    return due


def record_done(engine: sa.Engine, arrangement_id: str, kind: str, actor: str,
                detail: str = "") -> None:
    if kind not in ("withdrawal_acknowledged", "refund_made", "providers_notified", "form_sent"):
        raise OfaError(f"Unknown step {kind}")
    with engine.begin() as c:
        _arrangement(c, arrangement_id)
        _event(c, arrangement_id, kind, actor, detail)


# ----------------------------------------------------------------------------
# The worklist
# ----------------------------------------------------------------------------

def _status_of(c, a: dict, today: dt.date) -> ofa.Status:
    consents = [r[0] for r in c.execute(sa.select(db.ofa_consent.c.anniversary).where(
        db.ofa_consent.c.arrangement_id == a["id"]))]
    kinds = {r[0] for r in c.execute(sa.select(db.ofa_event.c.kind).where(
        db.ofa_event.c.arrangement_id == a["id"]))}
    n_accounts = c.execute(sa.select(sa.func.count()).select_from(db.ofa_account).where(
        db.ofa_account.c.arrangement_id == a["id"])).scalar()
    return ofa.status(a["reference_day"], consents, today, arrangement_status=a["status"],
                      providers_notified="providers_notified" in kinds,
                      has_accounts=bool(n_accounts),
                      consent_history_known=not (a["needs_review"] or "").startswith(
                          CONSENT_HISTORY))


ORDER = {"lapsed": 0, "urgent": 1, "due_soon": 2, "open": 3, "upcoming": 4, "check": 5,
         "covered": 6, "ended": 7}


def worklist(engine: sa.Engine, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    fg = db.family_group
    out = []
    with engine.connect() as c:
        rows = c.execute(sa.select(db.ofa_arrangement, fg.c.name.label("client"),
                                   fg.c.status.label("client_status"))
                         .join(fg, fg.c.id == db.ofa_arrangement.c.family_group_id)).mappings()
        for r in rows:
            a = dict(r)
            st = _status_of(c, a, today)
            accounts = [dict(x) for x in c.execute(sa.select(db.ofa_account).where(
                db.ofa_account.c.arrangement_id == a["id"])).mappings()]
            out.append({"arrangement": a, "status": st, "accounts": accounts})
    out.sort(key=lambda x: (ORDER.get(x["status"].state, 9),
                            x["status"].closes or dt.date.max, x["arrangement"]["client"]))
    return out


def write_worklist(items: Iterable[dict], path, today: dt.date) -> dict[str, int]:
    """The worklist as an Excel file: what to do, by when, for whom."""
    import xlsxwriter

    items = list(items)
    wb = xlsxwriter.Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False})
    head = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1, "text_wrap": True})
    date = wb.add_format({"num_format": "dd mmm yyyy"})
    money = wb.add_format({"num_format": "$#,##0"})
    colours = {"lapsed": "#F8CBAD", "urgent": "#FCE4D6", "due_soon": "#FFF2CC",
               "open": "#E2EFDA", "check": "#EDEDED"}
    ws = wb.add_worksheet("Ongoing fee consents")
    cols = ["Client", "Status", "What to do", "Due", "Anniversary", "Window opens",
            "Window closes", "Days left", "Last consent", "Annual fee", "Adviser",
            "Deduction accounts", "Check", "Arrangement"]
    for i, h in enumerate(cols):
        ws.write(0, i, h, head)
    counts: dict[str, int] = {}
    r = 0
    for r, item in enumerate(items, 1):
        a, st = item["arrangement"], item["status"]
        counts[st.state] = counts.get(st.state, 0) + 1
        fill = wb.add_format({"bg_color": colours[st.state]}) if st.state in colours else None
        ws.write(r, 0, a["client"])
        ws.write(r, 1, st.label, fill)
        ws.write(r, 2, st.action)
        for col, value in ((3, st.due_on), (4, st.anniversary), (5, st.opens), (6, st.closes),
                           (8, st.last_consent)):
            if value:
                ws.write_datetime(r, col, dt.datetime.combine(value, dt.time()), date)
        if st.days_left is not None:
            ws.write_number(r, 7, st.days_left)
        if a["fee_annual"] is not None:
            ws.write_number(r, 9, float(a["fee_annual"]), money)
        ws.write(r, 10, a["adviser"] or "")
        ws.write(r, 11, "; ".join(f"{x['provider']} {x['account_number'] or ''}".strip()
                                  for x in item["accounts"]))
        ws.write(r, 12, "; ".join(st.problems + ([a["needs_review"]] if a["needs_review"]
                                                 and a["needs_review"] not in st.problems
                                                 and not a["needs_review"].startswith(
                                                     CONSENT_HISTORY) else [])))
        ws.write(r, 13, a["id"])
    widths = [30, 22, 60, 12, 12, 12, 12, 9, 12, 11, 20, 34, 50, 26]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    ws.freeze_panes(1, 1)
    ws.autofilter(0, 0, max(r, 1), len(cols) - 1)
    ws.write(r + 2, 0, f"As at {today:%d %B %Y}. Window: 60 days before to "
             f"150 days after each anniversary (ASIC INFO 286). No consent in the window ends the "
             f"arrangement; tell account providers within 10 business days.")
    wb.close()
    return counts
