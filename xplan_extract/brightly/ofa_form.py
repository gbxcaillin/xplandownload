"""The annual ongoing fee consent form (renewal and deduction in one, s962YA), as a Word
document ready for e-signature. It carries everything ASIC INFO 286 lists for the form:
who the fee goes to and how to reach them, why consent is asked, how long it lasts, the
services, each fee and how it is worked out, how often it is charged, that the client can end
it any time, that it ends (and when) without consent, and for each account the holder names,
account number and amount. Every holder of a joint account signs.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field

from . import ofa


@dataclass
class Practice:
    name: str = "Prosperum Wealth"
    abn: str = ""
    afsl: str = ""                       # licensee and AFSL / authorised representative number
    address: str = ""
    phone: str = ""
    email: str = ""

    @classmethod
    def from_env(cls) -> "Practice":
        return cls(name=os.environ.get("PRACTICE_NAME", cls.name),
                   abn=os.environ.get("PRACTICE_ABN", ""),
                   afsl=os.environ.get("PRACTICE_AFSL", ""),
                   address=os.environ.get("PRACTICE_ADDRESS", ""),
                   phone=os.environ.get("PRACTICE_PHONE", ""),
                   email=os.environ.get("PRACTICE_EMAIL", ""))


DEFAULT_SERVICES = [
    "An annual review of your financial position, goals and strategy",
    "Review of your investments, super and insurance, with recommendations where needed",
    "Access to your adviser for questions during the year",
    "Ongoing monitoring of your accounts and portfolio",
]


@dataclass
class FormData:
    client_names: list[str]
    adviser: str
    anniversary: dt.date
    fee_annual: float | None
    fee_basis: str = ""
    frequency: str = "monthly"
    services: list[str] = field(default_factory=lambda: list(DEFAULT_SERVICES))
    accounts: list[dict] = field(default_factory=list)   # provider, account_name, number, amount, holders


def build(form: FormData, practice: Practice, path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    w = ofa.window(form.anniversary)
    lasts_until = ofa.window(ofa.anniversary(form.anniversary, form.anniversary.year + 1)).closes
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Calibri", Pt(10.5)

    def heading(text, size=13):
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.bold, run.font.size = True, Pt(size)
        run.font.color.rgb = RGBColor(0x0F, 0x6E, 0x6A)
        return p

    p = heading(practice.name, 11)
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    heading("Ongoing fee arrangement: renewal and consent to deduct fees", 15)
    doc.add_paragraph(f"For: {' and '.join(form.client_names)}")
    doc.add_paragraph(f"Arrangement anniversary: {form.anniversary:%d %B %Y}")

    heading("Why we are asking")
    doc.add_paragraph(
        "The law requires your written consent each year for us to keep providing ongoing advice "
        "services for a fee, and to deduct that fee from your account(s). This form renews the "
        "arrangement for the next 12 months and gives that consent.")

    heading("Who the fee is paid to")
    lines = [f"{practice.name}" + (f", ABN {practice.abn}" if practice.abn else ""),
             practice.afsl, practice.address,
             " · ".join(x for x in (practice.phone, practice.email) if x),
             f"Your adviser: {form.adviser}" if form.adviser else ""]
    for line in (l for l in lines if l):
        doc.add_paragraph(line)

    heading("Services you will receive over the next 12 months")
    for s in form.services:
        doc.add_paragraph(s, style="List Bullet")

    heading("Fees over the next 12 months")
    if form.fee_annual is not None:
        doc.add_paragraph(f"Total ongoing fee: ${form.fee_annual:,.2f} a year (including GST where it applies), "
                          f"charged {form.frequency}.")
    else:
        doc.add_paragraph("Total ongoing fee: (to be completed).")
    if form.fee_basis:
        doc.add_paragraph(f"How it is worked out: {form.fee_basis}")

    heading("Accounts the fee is deducted from")
    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Grid Accent 1"
    for cell, text in zip(table.rows[0].cells, ("Provider and account", "Account number",
                                                 "Account holder(s)", "Amount a year")):
        cell.text = text
    for a in form.accounts or [{}]:
        cells = table.add_row().cells
        cells[0].text = " · ".join(x for x in (a.get("provider"), a.get("account_name")) if x)
        cells[1].text = a.get("account_number") or ""
        cells[2].text = a.get("holders") or ""
        amount = a.get("amount_annual")
        cells[3].text = f"${float(amount):,.2f}" if amount not in (None, "") else ""
    doc.add_paragraph("A copy of this consent will be given to each account provider. If an "
                      "account is held jointly, every holder must sign.")

    heading("Important")
    for text in (
        f"This consent lasts until {lasts_until:%d %B %Y} at the latest. We will ask you to "
        f"renew it again around the next anniversary.",
        "You can end this arrangement, or withdraw your consent to fees being deducted, at any "
        "time by telling us in writing. No further fees will be charged after that.",
        f"If you do not sign this form by {w.closes:%d %B %Y}, the arrangement will end on that "
        f"date, our ongoing services will stop and no further ongoing fees will be charged.",
    ):
        doc.add_paragraph(text, style="List Bullet")

    heading("Your consent")
    doc.add_paragraph("I/We renew the ongoing fee arrangement on the terms above for the next "
                      "12 months, and consent to the fees being deducted from the account(s) "
                      "listed.")
    sig = doc.add_table(rows=1, cols=3)
    for cell, text in zip(sig.rows[0].cells, ("Name", "Signature", "Date")):
        cell.text = text
    for name in form.client_names:
        cells = sig.add_row().cells
        cells[0].text = name
    doc.add_paragraph()
    note = doc.add_paragraph(f"Please sign between {w.opens:%d %B %Y} and {w.closes:%d %B %Y}.")
    note.runs[0].italic = True
    doc.save(str(path))


def form_for(engine, arrangement_id: str, anniversary: dt.date | None = None,
             today: dt.date | None = None) -> FormData:
    """The form's contents from the Brightly record."""
    import sqlalchemy as sa
    from . import database as db
    from . import ofa_store

    today = today or dt.date.today()
    with engine.connect() as c:
        a = ofa_store._arrangement(c, arrangement_id)
        if anniversary is None:
            st = ofa_store._status_of(c, a, today)
            anniversary = st.anniversary or ofa.anniversary(a["reference_day"], today.year)
        people = [r[0] for r in c.execute(
            sa.select(db.person.c.name).where(db.person.c.family_group_id == a["family_group_id"])
            .order_by(db.person.c.position)) if r[0]]
        accounts = [dict(r) for r in c.execute(sa.select(db.ofa_account).where(
            db.ofa_account.c.arrangement_id == arrangement_id)).mappings()]
    return FormData(client_names=people or ["(client)"], adviser=a["adviser"] or "",
                    anniversary=anniversary,
                    fee_annual=float(a["fee_annual"]) if a["fee_annual"] is not None else None,
                    fee_basis=a["fee_basis"] or "", frequency=a["frequency"] or "monthly",
                    services=[s.strip() for s in (a["services"] or "").split("\n") if s.strip()]
                    or list(DEFAULT_SERVICES), accounts=accounts)
