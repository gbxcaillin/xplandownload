"""Build Brightly records (households, prospects, entities, tasks) from the restored Xplan database.

Xplan layout used here (Banyan's Xplan, see the profile):

* Every person, SMSF, trust and company is an *entity*. Its single-value fields are spread over
  the ``ufield_entity#..`` tables (one row per entity, key ``eidobj``).
* ``entity_clients`` lists the client entities. Couples: ``clientrelation_marrying``
  (subject = client, object = partner) and ``partner_entity_id``.
* A couple's financial lists (assets, funds, cash flow) sit on the client's record with
  owner Client / Partner / Joint.
* SMSF/trust/company roles: ``ufield_entity_trustee`` / ``_director`` / ``_fundmember`` /
  ``_shareholder`` / ``_beneficiary`` / ``_settlor`` (``eidobj`` = the structure, ``*_entityid``
  = the person or company in the role).
* Status labels (client / prospect ...) are in ``_multidata`` (field ``entity_client_status``).

Anything filled in for at least one record that isn't used below is listed in unmapped.csv.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

from . import (FactFind, Schema, Unmapped, clean_date, clean_number, clean_text, clean_yesno,
               is_tfn_column)

Progress = Callable[[str], None]

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Australia/Melbourne")
except Exception:  # no time zone data: fall back to AEST
    _TZ = dt.timezone(dt.timedelta(hours=10))

RISK_OPTIONS = ["Conservative", "Moderate", "Balanced", "Growth", "High Growth"]
TITLE_MAP = {"mr": "Mr", "mr.": "Mr", "mrs": "Mrs", "mrs.": "Mrs", "ms": "Ms", "ms.": "Ms",
             "miss": "Miss"}
MARITAL_MAP = {"married": "Married", "single": "Single", "de facto": "Defacto",
               "defacto": "Defacto", "widowed": "Widowed", "divorced": "Divorced"}
EMPLOYMENT_MAP = {"full-time": "Full time", "full time": "Full time", "part-time": "Part time",
                  "part time": "Part time", "casual": "Casual", "self employed": "Self employed",
                  "self-employed": "Self employed", "unemployed": "Unemployed",
                  "retired": "Retired"}
STATE_MAP = {"vic": "VIC", "victoria": "VIC", "nsw": "NSW", "new south wales": "NSW",
             "act": "ACT", "australian capital territory": "ACT", "qld": "QLD",
             "queensland": "QLD", "nt": "NT", "northern territory": "NT", "wa": "WA",
             "western australia": "WA", "sa": "SA", "south australia": "SA", "tas": "TAS",
             "tasmania": "TAS"}
STRUCTURE_TYPES = {"superfund": "SMSF", "company": "Company", "trust": "Trust"}
ROLE_TABLES = [  # table, person/company id column, Brightly role
    ("ufield_entity_trustee", "trustee_entityid", "Trustee"),
    ("ufield_entity_director", "director_entityid", "Director"),
    ("ufield_entity_fundmember", "fundmember_entityid", "Member"),
    ("ufield_entity_shareholder", "shareholder_entityid", "Shareholder"),
    ("ufield_entity_beneficiary", "beneficiary_entityid", "Beneficiary"),
    ("ufield_entity_settlor", "settlor_entityid", "Settlor"),
]
RELATION_ROLES = {"trustee": "Trustee", "director": "Director", "super": "Member",
                  "member": "Member", "beneficiary": "Beneficiary", "shareholder": "Shareholder",
                  "appointor": "Appointor", "settlor": "Settlor"}
TECHNICAL = {"index", "eidobj", "_parentproc_dumplog", "dumplogcsv", "id", "guid", "verid",
             "listitemid", "created_at", "created_by", "modified_at", "modified_by",
             "modifiedby", "modifiedstamp", "createdstamp", "values_last_updated",
             "update_from_datafeed", "integration_indicator", "entity_id", "entityid",
             "client_id", "clientid", "related_id", "subjectid", "objectid"}


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------

def tfn_like_sql(column: str) -> str:
    """SQL condition: the column holds something shaped like a TFN (8-9 digits only)."""
    v = f"REPLACE(REPLACE(LTRIM(RTRIM(CAST([{column}] AS nvarchar(50)))), ' ', ''), '-', '')"
    return f"(LEN({v}) BETWEEN 8 AND 9 AND {v} NOT LIKE '%[^0-9]%')"


def qname(table: str) -> str:
    schema, _, name = table.rpartition(".")
    return f"[{(schema or 'dbo').replace(']', ']]')}].[{name.replace(']', ']]')}]"


class XplanDb:
    def __init__(self, conn):
        self.conn = conn
        self._columns: dict[str, dict[str, str]] = {}
        self.tables = {r[0].lower(): r[0] for r in conn.cursor().execute(
            "SELECT name FROM sys.tables WHERE is_ms_shipped = 0")}

    def exists(self, table: str) -> bool:
        return table.lower() in self.tables

    def columns(self, table: str) -> dict[str, str]:
        """lower-case name -> actual name"""
        key = table.lower()
        if key not in self._columns:
            if not self.exists(table):
                self._columns[key] = {}
            else:
                rows = self.conn.cursor().execute(
                    "SELECT c.name FROM sys.columns c WHERE c.object_id = OBJECT_ID(?)",
                    qname(self.tables[key])).fetchall()
                self._columns[key] = {r[0].lower(): r[0] for r in rows}
        return self._columns[key]

    def rows(self, table: str, wanted: Iterable[str], where: str = "",
             params: tuple = ()) -> list[dict]:
        """Rows with the wanted columns that exist (missing ones come back as None)."""
        cols = self.columns(table)
        if not cols:
            return []
        wanted = list(wanted)
        present = [w for w in wanted if w.lower() in cols]
        if not present:
            return []
        select = ", ".join(f"[{cols[w.lower()]}]" for w in present)
        cur = self.conn.cursor()
        cur.execute(f"SELECT {select} FROM {qname(self.tables[table.lower()])} {where}", params)
        out = []
        for r in cur.fetchall():
            d = dict.fromkeys(wanted)
            d.update(zip(present, r))
            out.append(d)
        return out

    def has_value_sql(self, table: str, column: str) -> str | None:
        cols = self.columns(table)
        c = cols.get(column.lower())
        return f"[{c}]" if c else None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        from ..documents import decode_text
        value = decode_text(bytes(value))
    text = str(value)
    if "<" in text and ">" in text:
        p = _Text()
        try:
            p.feed(text)
            p.close()
            text = "".join(p.parts)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\r", "")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def iso_at(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        if value.year < 1900:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=_TZ)
        return value.isoformat(timespec="seconds")
    d = clean_date(value)
    return f"{d}T00:00:00+10:00" if d else None


def person_name(first: Any, last: Any, fallback: Any = None) -> str:
    first, last = clean_text(first), clean_text(last)
    if first or last:
        return " ".join(x for x in (first, last) if x)
    fb = clean_text(fallback) or ""
    if "," in fb:  # Xplan "Last, First"
        last, _, first = fb.partition(",")
        return f"{first.strip()} {last.strip()}".strip()
    return fb


def staff_name(value: Any) -> str | None:
    """Xplan users are "Last, First"."""
    text = clean_text(value)
    if not text:
        return None
    if "," in text:
        last, _, first = text.partition(",")
        return f"{first.strip()} {last.strip()}".strip()
    return text


def first_value(*values):
    for v in values:
        if v not in (None, "", 0) and not (isinstance(v, str) and not v.strip()):
            return v
    return None


def money(value: Any) -> int | float | None:
    num = clean_number(value)
    return num if num not in (None, 0) else None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

ENTITY_FIELDS = [
    "entity_id", "entity_name", "entity_type", "first_name", "middle_name", "last_name",
    "title", "dob", "marital_status", "jobtitle", "occupation",
    "employer", "emp_status", "country_of_birth", "partner_entity_id", "client_adviser",
    "client_active_date", "create_date", "company_name", "company_number",
    "abn", "accountant", "superfund_name", "superfund_number", "trust_name",
    "trust_number", "trust_type", "tfn_supplied", "will_exists", "date_of_will", "smoker",
    "risk_profile", "risk_profile_j", "disclosure_statement_date", "preferred_email",
    "preferred_phone", "preferred_suburb", "preferred_street", "preferred_state",
    "preferred_postcode", "referral_source",
]


@dataclass
class Entity:
    id: int
    f: dict
    labels: list[str] = field(default_factory=list)

    @property
    def type(self) -> str:
        return (clean_text(self.f.get("entity_type")) or "").lower()

    @property
    def name(self) -> str:
        if self.type == "individual":
            return person_name(self.f.get("first_name"), self.f.get("last_name"),
                               self.f.get("entity_name"))
        return clean_text(first_value(self.f.get("superfund_name"), self.f.get("trust_name"),
                                      self.f.get("company_name"), self.f.get("entity_name"))) or ""


class XplanData:
    """Everything the mapping needs, loaded once."""

    def __init__(self, db: XplanDb, progress: Progress):
        self.db = db
        self.mapped: dict[str, set[str]] = defaultdict(set)   # table -> columns used
        progress("Loading entities ...")
        self.entities = self._load_entities()
        self.clients = {r["entity_id"] for r in db.rows("entity_clients", ["entity_id"])}
        self._use("entity_clients", "entity_id", "client_adviser", "entity_name", "entity_type")
        self._load_labels()
        self.users = self._load_users()
        self.partner_of = self._load_partners()
        progress("Loading contacts, addresses, reviews ...")
        self.contacts = self._group("ufield_entity_contact",
                                    ["eidobj", "type", "value", "preferred"])
        self.addresses = self._group("ufield_entity_address",
                                     ["eidobj", "type", "street", "suburb", "state", "postcode",
                                      "preferred", "country"])
        self.reviews = self._group("ufield_entity_review",
                                   ["eidobj", "date", "date_completed", "status", "type"])
        self.employment = self._group("ufield_entity_employment",
                                      ["eidobj", "jobtitle", "occupation", "employer",
                                       "emp_status", "ordinary_wages", "primary_employment",
                                       "main_occupation"])
        self.dependants = self._group("ufield_entity_dependent",
                                      ["eidobj", "dep_age", "dep_until_age", "dep_rel",
                                       "dep_related_to", "financ_dep"])
        progress("Loading assets, liabilities, funds ...")
        self.assets = self._group("entity_assets",
                                  ["eidobj", "type", "type_group", "desc", "owner", "amount",
                                   "status"])
        self.liabs = self._group("entity_liabilities",
                                 ["eidobj", "type", "type_group", "account", "owner", "amount",
                                  "status"])
        self.funds = self._group("ufield_entity_fund",
                                 ["eidobj", "fund_name", "fund_super_plan", "fund_refnum",
                                  "fund_total_balance", "date_of_balance", "fund_type",
                                  "fund_status", "owner_type", "fund_tax_free",
                                  "fund_taxable_untaxed", "life_cover", "tpd_cover",
                                  "salary_continuance", "supplier"])
        self.pensions = self._group("ufield_entity_retirement_income",
                                    ["eidobj", "provider_name", "provider", "account_num",
                                     "pension_balance", "type", "pension_status", "desc"])
        self.goals = self._load_goals()
        self.fees = self._group("ufield_entity_NAG_fees_cust",
                                ["eidobj", "type", "description", "manual_fee_type",
                                 "fee_total_amount", "value"])
        self.fds = self._load_fds()
        progress("Loading platform accounts ...")
        self.platform = self._load_platform_accounts()
        progress("Loading SMSF / trust / company roles ...")
        self.roles = self._load_roles()
        self.ins_renewal = self._load_insurance_renewals()
        self.relations = self._load_relations()

    # -- generic ----------------------------------------------------------
    def _use(self, table: str, *cols: str) -> None:
        self.mapped[table.lower()].update(c.lower() for c in cols)

    def _group(self, table: str, cols: list[str], key: str = "eidobj") -> dict[int, list[dict]]:
        self._use(table, *cols)
        out: dict[int, list[dict]] = defaultdict(list)
        for r in self.db.rows(table, cols):
            if r.get(key) is not None:
                out[int(r[key])].append(r)
        return out

    # -- entities ---------------------------------------------------------
    def _load_entities(self) -> dict[int, Entity]:
        chunks = sorted((t for t in self.db.tables.values()
                         if re.fullmatch(r"ufield_entity(_cust)?#\w+", t, re.I)),
                        key=lambda t: ("_cust" in t.lower(), t))
        where_col: dict[str, str] = {}
        for t in chunks:
            for low in self.db.columns(t):
                if low in (f.lower() for f in ENTITY_FIELDS) and low not in where_col:
                    where_col[low] = t
        ents: dict[int, Entity] = {}
        by_table: dict[str, list[str]] = defaultdict(list)
        for f in ENTITY_FIELDS:
            if f.lower() in where_col:
                by_table[where_col[f.lower()]].append(f)
        for t, fields in by_table.items():
            self._use(t, *fields)
            for r in self.db.rows(t, ["eidobj", *fields]):
                if r["eidobj"] is None:
                    continue
                eid = int(r["eidobj"])
                e = ents.setdefault(eid, Entity(eid, {}))
                for k in fields:
                    if e.f.get(k) in (None, "") and r[k] not in (None, ""):
                        e.f[k] = r[k]
        # whether a TFN is held - computed in SQL so the number itself is never read
        self.tfn_held: dict[int, bool] = {}
        self.tfn_columns: list[tuple[str, str]] = []
        for t in chunks + [x for x in self.db.tables.values()
                           if x.lower().startswith("ufield_entity")]:
            for low, actual in self.db.columns(t).items():
                if is_tfn_column(low) and not low.startswith("tfn_supplied"):
                    if (t, actual) in self.tfn_columns:
                        continue
                    self.tfn_columns.append((t, actual))
                    if t in chunks and "eidobj" in self.db.columns(t):
                        cur = self.db.conn.cursor()
                        cur.execute(f"SELECT eidobj FROM {qname(t)} WHERE {tfn_like_sql(actual)}")
                        for (eid,) in cur.fetchall():
                            self.tfn_held[int(eid)] = True
        return ents

    def _load_labels(self) -> None:
        if not self.db.exists("_multidata"):
            return
        self._use("_multidata", "group", "entityid", "field", "value", "value_text")
        for r in self.db.rows("_multidata", ["entityid", "field", "value_text"],
                              "WHERE [group] = 'entity' AND field = 'entity_client_status'"):
            e = self.entities.get(int(r["entityid"] or 0))
            if e and clean_text(r["value_text"]):
                e.labels.append(clean_text(r["value_text"]))

    def _load_users(self) -> dict[int, str]:
        """Xplan user id -> "First Last", from the id/label pairs on records."""
        users: dict[int, str] = {}
        pairs = [("entity_clients", "client_adviser"), ("ufield_entity_review", "adviser")]
        pairs += [(t, c) for t in self.db.tables.values()
                  if re.fullmatch(r"ufield_entity#\w+", t, re.I)
                  for c in ("client_adviser", "client_adviser_administrator",
                            "client_adviser_secondary", "client_adviser_paraplanner",
                            "client_adviser_service")]
        for t, c in pairs:
            if c.lower() in self.db.columns(t) and f"{c}_vlu".lower() in self.db.columns(t):
                for r in self.db.rows(t, [c, f"{c}_vlu"], f"WHERE [{c}] IS NOT NULL"):
                    try:
                        uid = int(str(r[f"{c}_vlu"]).strip())
                    except (TypeError, ValueError):
                        continue
                    name = staff_name(r[c])
                    if name:
                        users.setdefault(uid, name)
        return users

    def user(self, uid: Any) -> str | None:
        try:
            return self.users.get(int(uid))
        except (TypeError, ValueError):
            return None

    def _load_partners(self) -> dict[int, int]:
        partner: dict[int, int] = {}
        self._use("clientrelation_marrying", "subjectid", "objectid")
        self.primary_of_couple: set[int] = set()
        for r in self.db.rows("clientrelation_marrying", ["subjectid", "objectid"]):
            if r["subjectid"] and r["objectid"]:
                a, b = int(r["subjectid"]), int(r["objectid"])
                partner[a], partner[b] = b, a
                self.primary_of_couple.add(a)
        for e in self.entities.values():
            p = e.f.get("partner_entity_id")
            if p and e.id not in partner:
                partner[e.id] = int(p)
        return partner

    def _load_goals(self) -> dict[int, list[tuple[str, Any]]]:
        out: dict[int, list[tuple[str, Any]]] = defaultdict(list)
        sources = [("ufield_entity_client_objectives_cust", ["eidobj", "description", "priority"]),
                   ("ufield_entity_goals", ["eidobj", "description", "capture_date"]),
                   ("ufield_entity_objectives", ["eidobj", "description", "capture_date"])]
        for t, cols in sources:
            self._use(t, *cols)
            rows = self.db.rows(t, cols)
            rows.sort(key=lambda r: clean_number(r.get("priority")) or 99)
            for r in rows:
                text = html_to_text(r.get("description"))
                if r["eidobj"] is not None and text:
                    out[int(r["eidobj"])].append((text, r.get("capture_date")))
        return out

    def _load_fds(self) -> dict[int, list[dict]]:
        cols = ["eidobj", "fee_amount", "fee_date", "revex_fee_description"]
        return self._group("ufield_entity_banyan_fds_cust", cols)

    def _load_platform_accounts(self) -> dict[int, list[dict]]:
        """Platform / wrap accounts by owning entity, valued from current positions."""
        values: dict[tuple, float] = defaultdict(float)
        self._use("ips_position", "portfolioid", "subfund", "value")
        for r in self.db.rows("ips_position", ["portfolioid", "subfund", "value"]):
            values[(r["portfolioid"], r["subfund"])] += float(r["value"] or 0)
        owners: dict[int, list[int]] = defaultdict(list)
        self._use("ips_subfund_visibility_link", "id", "entityid", "reason_for_visibility")
        for r in self.db.rows("ips_subfund_visibility_link",
                              ["id", "entityid", "reason_for_visibility"]):
            if r["id"] is not None and r["entityid"] is not None and \
                    r["reason_for_visibility"] in ("single_owner", "primary_joint_owner",
                                                   "secondary_joint_owner", "shared_owner"):
                owners[int(r["id"])].append(int(r["entityid"]))
        cols = ["ips_portfolio_id", "subfund", "description", "externalaccount",
                "externalvendorname", "linkCode", "subfund_account_name", "subfund_status",
                "taxStructure", "tax_type", "visibility_link", "subfund_is_super"]
        self._use("ips_subfund", *cols)
        out: dict[int, list[dict]] = defaultdict(list)
        for r in self.db.rows("ips_subfund", cols):
            status = (clean_text(r["subfund_status"]) or "").lower()
            bal = values.get((r["ips_portfolio_id"], r["subfund"]), 0.0)
            if status in ("closed", "terminating", "recommended") or (not bal and status != "active"):
                continue
            r["balance"] = round(bal, 2)
            r["owners"] = owners.get(int(r["visibility_link"] or 0), [])
            for eid in r["owners"][:1]:  # attach to the first (primary) owner
                out[eid].append(r)
        return out

    def _load_roles(self) -> dict[int, list[tuple[int, str, dict]]]:
        """structure entity id -> [(person/company id, role, row)]"""
        roles: dict[int, list[tuple[int, str, dict]]] = defaultdict(list)
        for table, col, role in ROLE_TABLES:
            extra = {"ufield_entity_trustee": ["trustee_type", "companyname"],
                     "ufield_entity_fundmember": ["fund_balance"]}.get(table, [])
            self._use(table, "eidobj", col, *extra)
            for r in self.db.rows(table, ["eidobj", col, *extra]):
                if r["eidobj"] is not None and r[col] is not None:
                    roles[int(r["eidobj"])].append((int(r[col]), role, r))
        return roles

    def _load_relations(self) -> dict[int, list[tuple[int, str]]]:
        """General Xplan relationships: entity -> [(other entity, label)], both directions."""
        out: dict[int, list[tuple[int, str]]] = defaultdict(list)
        self._use("relation_clientrelating", "subjectid", "objectid", "client_rel")
        for r in self.db.rows("relation_clientrelating", ["subjectid", "objectid", "client_rel"]):
            if r["subjectid"] and r["objectid"]:
                label = clean_text(r["client_rel"]) or ""
                a, b = int(r["subjectid"]), int(r["objectid"])
                out[a].append((b, label))
                out[b].append((a, label))
        return out

    def _load_insurance_renewals(self) -> dict[int, list[Any]]:
        out: dict[int, list[Any]] = defaultdict(list)
        cols = ["client_id", "renewal_date"]
        self._use("sections_insurance_insurancepolicycover", *cols)
        for r in self.db.rows("sections_insurance_insurancepolicycover", cols):
            if r["client_id"] is not None and r["renewal_date"]:
                out[int(r["client_id"])].append(r["renewal_date"])
        return out


# --------------------------------------------------------------------------
# Building records
# --------------------------------------------------------------------------

@dataclass
class Household:
    id: str
    people: list[Entity]              # 1 or 2
    record_entity: Entity             # where the couple's lists live (the client)
    accounts: list[dict] = field(default_factory=list)


def owner_pk(owner_label: Any, record: Entity, hh: Household) -> str | None:
    """Client / Partner / Joint on the record -> "1" / "2" / "joint"."""
    label = (clean_text(owner_label) or "").lower()
    if label == "joint":
        return "joint"
    ids = [p.id for p in hh.people]
    if label in ("client", ""):
        target = record.id
    elif label == "partner":
        others = [i for i in ids if i != record.id]
        target = others[0] if others else None
    else:
        return None
    return str(ids.index(target) + 1) if target in ids else None


class BrightlyBuilder:
    def __init__(self, data: XplanData, schema: Schema, today: dt.date | None = None):
        self.d = data
        self.schema = schema
        self.today = today or dt.date.today()
        self.problems: list[Unmapped] = []
        self.sensitive = 0
        self.households: dict[str, Household] = {}
        self.person_home: dict[int, tuple[str, str]] = {}   # person id -> (household id, pk)
        self.smsf_for: dict[str, list[dict]] = defaultdict(list)   # household -> SMSF facts
        self.directorships: dict[int, list[str]] = defaultdict(list)  # person -> company names
        self.counts = defaultdict(int)
        self._plan_households()

    # -- households ---------------------------------------------------------
    def _plan_households(self) -> None:
        d = self.d
        done: set[int] = set()
        for eid in sorted(d.clients):
            e = d.entities.get(eid)
            if not e or e.type != "individual" or eid in done:
                continue
            partner_id = d.partner_of.get(eid)
            partner = d.entities.get(partner_id) if partner_id else None
            if partner and partner.type != "individual":
                partner = None
            first, second = e, partner
            if partner and partner.id in d.primary_of_couple and eid not in d.primary_of_couple:
                first, second = partner, e
            people = [first] + ([second] if second else [])
            hid = f"H-{first.id}"
            self.households[hid] = Household(hid, people, first)
            for i, p in enumerate(people, 1):
                done.add(p.id)
                self.person_home[p.id] = (hid, str(i))

    def is_prospect(self, hh: Household) -> bool:
        labels = " ".join(hh.record_entity.labels).lower()
        return "prospect" in labels and "client" not in labels.replace("prospect", "")

    def household_record(self, hh: Household, notes: list[dict], advice: list[dict],
                         signed: dict) -> dict:
        d, rec = self.d, hh.record_entity
        problems: list[Unmapped] = []
        ff = FactFind(self.schema, problems)
        people = []
        for i, p in enumerate(hh.people, 1):
            emp = self._employment(p)
            income = money(emp.get("ordinary_wages")) if emp else None
            job = clean_text(first_value(emp.get("jobtitle") if emp else None,
                                         emp.get("occupation") if emp else None,
                                         p.f.get("jobtitle"), p.f.get("occupation")))
            people.append({"n": p.name, "role": "Client" if i == 1 else "Partner",
                           "dob": clean_date(p.f.get("dob")), "job": job, "income": income})
            self._person_ff(ff, p, i, emp)

        emails, phones = self._contacts(hh)
        addr = self._address(rec)
        reviews = [r for p in hh.people for r in d.reviews.get(p.id, [])]
        done = [r["date_completed"] for r in reviews
                if (clean_text(r["status"]) or "").lower() == "completed" and r["date_completed"]]
        upcoming = [r["date"] for r in reviews
                    if (clean_text(r["status"]) or "").lower() not in ("completed", "cancelled")
                    and r["date"]]
        fee, fee_type = self._fee(hh)
        risk_j = self._risk(rec.f.get("risk_profile_j"), "risk_profile_j")
        risk_1 = self._risk(rec.f.get("risk_profile"), "risk_profile")
        if len(hh.people) == 2 and risk_j:
            ff.set("riskProfileJoint", risk_j, source="ufield_entity.risk_profile_j")
        renewals = [r for p in hh.people for r in d.ins_renewal.get(p.id, [])
                    if clean_date(r) and clean_date(r) >= self.today.isoformat()]
        self._assets_ff(ff, hh)
        self._funds(ff, hh)
        self._dependants_ff(ff, hh)
        self._smsf_ff(ff, hh)
        for i, p in enumerate(hh.people, 1):
            if self.directorships.get(p.id):
                ff.set("director", "; ".join(self.directorships[p.id]), person=i)
        if (m := MARITAL_MAP.get((clean_text(rec.f.get("marital_status")) or "").lower())):
            ff.set("marital", m)
        elif rec.f.get("marital_status"):
            self._problem("ufield_entity", "marital_status", "marital",
                          f"value '{clean_text(rec.f.get('marital_status'))}' has no Brightly option")

        goals = []
        for p in hh.people:
            for text, date in d.goals.get(p.id, []):
                if text not in (g[0] for g in goals):
                    goals.append((text, date))
        self.sensitive += ff.sensitive_count
        self.problems.extend(problems)

        names = [p.name for p in hh.people]
        if len(hh.people) == 2:
            l1 = clean_text(hh.people[0].f.get("last_name"))
            l2 = clean_text(hh.people[1].f.get("last_name"))
            f1 = clean_text(hh.people[0].f.get("first_name")) or names[0]
            f2 = clean_text(hh.people[1].f.get("first_name")) or names[1]
            name = f"{f1} & {f2} {l1}" if l1 and l1 == l2 else " & ".join(names)
        else:
            name = names[0]
        status = ", ".join(rec.labels) or "no status"
        record = {
            "id": hh.id,
            "ext": {"xplan": str(rec.id)},
            "name": name,
            "type": "Couple" if len(hh.people) == 2 else "Individual",
            "adviser": staff_name(rec.f.get("client_adviser")),
            "since": clean_date(first_value(rec.f.get("client_active_date"),
                                            rec.f.get("create_date"))),
            "email": emails[0]["addr"] if emails else None,
            "phone": phones[0]["num"] if phones else None,
            "city": addr.get("suburb"),
            "notes": "",
            "lastReview": clean_date(max(done)) if done else None,
            "nextReview": clean_date(min(upcoming)) if upcoming else None,
            "ofa": clean_date(rec.f.get("disclosure_statement_date")),
            "insRenewal": clean_date(min(renewals)) if renewals else None,
            "fee": fee,
            "feeType": fee_type,
            "profile": {
                "people": people,
                "accounts": hh.accounts,
                "assets": self._asset_list(hh, d.assets, "desc"),
                "liabs": self._asset_list(hh, d.liabs, "account"),
                "goals": [g[0] for g in goals],
                "goalMeta": [{"date": clean_date(g[1]), "source": "Xplan"} for g in goals],
                "risk": risk_j if len(hh.people) == 2 and risk_j else risk_1,
                "adviceHist": advice,
            },
            "contacts": {"emails": emails, "phones": phones},
            "fileNotes": notes,
            "ff": ff.as_dict(),
            "signed": signed,
            "log": [{"at": dt.datetime.now(_TZ).isoformat(timespec="seconds"),
                     "by": "Xplan import", "kind": "Imported",
                     "m": f"Imported from Xplan client {rec.id} (Xplan status: {status})"}],
        }
        if self.is_prospect(hh):
            record.update({"prospect": True,
                           "source": clean_text(rec.f.get("referral_source")),
                           "added": clean_date(rec.f.get("create_date"))})
        return record

    # -- people -------------------------------------------------------------
    def _employment(self, p: Entity) -> dict:
        rows = self.d.employment.get(p.id, [])
        rows.sort(key=lambda r: (not r.get("primary_employment"), not r.get("main_occupation")))
        return rows[0] if rows else {}

    def _person_ff(self, ff: FactFind, p: Entity, i: int, emp: dict) -> None:
        f = p.f
        title = TITLE_MAP.get((clean_text(f.get("title")) or "").lower())
        if title:
            ff.set("title", title, person=i)
        elif f.get("title"):
            self._problem("ufield_entity", "title", "title",
                          f"value '{clean_text(f.get('title'))}' has no Brightly option")
        ff.set("firstName", f.get("first_name"), person=i)
        ff.set("middleNames", f.get("middle_name"), person=i)
        ff.set("lastName", f.get("last_name"), person=i)
        ff.set("dob", f.get("dob"), person=i)
        mobile = self._contact_value(p, ("mobile",))
        email = self._contact_value(p, ("email",)) or clean_text(f.get("preferred_email"))
        ff.set("mobile", mobile, person=i)
        ff.set("email", email, person=i)
        addr = self._address(p)
        ff.set("address", addr.get("street"), person=i)
        ff.set("suburb", addr.get("suburb"), person=i)
        ff.set("state", addr.get("state"), person=i)
        ff.set("postcode", addr.get("postcode"), person=i)
        ff.set("countryOfBirth", f.get("country_of_birth"), person=i)
        ff.set("jobTitle", first_value(emp.get("jobtitle"), f.get("jobtitle")), person=i)
        ff.set("occupation", first_value(emp.get("occupation"), f.get("occupation")), person=i)
        ff.set("employer", first_value(emp.get("employer"), f.get("employer")), person=i)
        status = clean_text(first_value(emp.get("emp_status"), f.get("emp_status")))
        if status:
            mapped = EMPLOYMENT_MAP.get(status.lower())
            if mapped:
                ff.set("employmentStatus", mapped, person=i)
            else:
                self._problem("ufield_entity_employment", "emp_status", "employmentStatus",
                              f"value '{status}' has no Brightly option")
        ff.set("grossIncome", money(emp.get("ordinary_wages")), person=i)
        risk = self._risk(f.get("risk_profile"), "risk_profile")
        if risk:
            ff.set("riskProfile", risk, person=i)
        will = f.get("will_exists")
        if will is not None:
            ff.set("hasWill", "Yes" if will in (1, True, "1") else "No", person=i)
        if clean_date(f.get("date_of_will")):
            ff.set("willYear", clean_date(f.get("date_of_will"))[:4], person=i)
        smoker = clean_yesno(f.get("smoker"))
        if smoker:
            ff.set("smoker12m", smoker, person=i)

    def _risk(self, value: Any, column: str) -> str | None:
        text = clean_text(value)
        if not text:
            return None
        for opt in RISK_OPTIONS:
            if opt.lower() == text.lower():
                return opt
        self._problem("ufield_entity", column, "risk / riskProfile",
                      f"value '{text}' has no Brightly option")
        return None

    def _contact_value(self, p: Entity, kinds: tuple[str, ...]) -> str | None:
        rows = [r for r in self.d.contacts.get(p.id, [])
                if any(k in (clean_text(r["type"]) or "").lower() for k in kinds)
                and clean_text(r["value"])]
        rows.sort(key=lambda r: not r["preferred"])
        return clean_text(rows[0]["value"]) if rows else None

    def _contacts(self, hh: Household) -> tuple[list[dict], list[dict]]:
        emails, phones = [], []
        for p in hh.people:
            rows = sorted(self.d.contacts.get(p.id, []), key=lambda r: not r["preferred"])
            for r in rows:
                kind = (clean_text(r["type"]) or "").lower()
                value = clean_text(r["value"])
                if not value or "fax" in kind:
                    continue
                if "email" in kind or "@" in value:
                    if value.lower() not in (e["addr"].lower() for e in emails):
                        emails.append({"addr": value, "primary": False})
                elif any(k in kind for k in ("phone", "mobile")):
                    if value not in (x["num"] for x in phones):
                        phones.append({"num": value, "primary": False})
            fallback_email = clean_text(p.f.get("preferred_email"))
            if fallback_email and "@" in fallback_email and \
                    fallback_email.lower() not in (e["addr"].lower() for e in emails):
                emails.append({"addr": fallback_email, "primary": False})
            fallback_phone = clean_text(p.f.get("preferred_phone"))
            if fallback_phone and fallback_phone not in (x["num"] for x in phones):
                phones.append({"num": fallback_phone, "primary": False})
        if emails:
            emails[0]["primary"] = True
        if phones:
            phones[0]["primary"] = True
        return emails, phones

    def _address(self, p: Entity) -> dict:
        rows = self.d.addresses.get(p.id, [])
        rows = sorted(rows, key=lambda r: (not r["preferred"],
                                           (clean_text(r["type"]) or "") != "Residential"))
        r = rows[0] if rows else {"street": p.f.get("preferred_street"),
                                  "suburb": p.f.get("preferred_suburb"),
                                  "state": p.f.get("preferred_state"),
                                  "postcode": p.f.get("preferred_postcode")}
        suburb = clean_text(r.get("suburb"))
        if suburb and suburb.isupper():
            suburb = suburb.title()
        state = STATE_MAP.get((clean_text(r.get("state")) or "").lower())
        return {"street": clean_text(r.get("street")), "suburb": suburb, "state": state,
                "postcode": clean_text(r.get("postcode"))}

    # -- money ----------------------------------------------------------------
    def _fee(self, hh: Household) -> tuple[Any, str | None]:
        rows = [r for p in hh.people for r in self.d.fees.get(p.id, [])]
        ongoing = [r for r in rows if "ongoing" in (clean_text(r["type"]) or "").lower()
                   or "ongoing" in (clean_text(r["description"]) or "").lower()]
        fixed = [money(first_value(r["fee_total_amount"], r["value"])) for r in ongoing
                 if (clean_text(r["manual_fee_type"]) or "").lower() == "fixed"]
        fixed = [f for f in fixed if f]
        if fixed:
            return round(sum(fixed), 2), "Ongoing"
        # Otherwise: ongoing fees actually received over the last 12 months (Banyan FDS).
        since = self.today - dt.timedelta(days=365)
        received = [float(r["fee_amount"] or 0) for p in hh.people for r in self.d.fds.get(p.id, [])
                    if "ongoing" in (clean_text(r["revex_fee_description"]) or "").lower()
                    and isinstance(r["fee_date"], (dt.date, dt.datetime))
                    and (r["fee_date"].date() if isinstance(r["fee_date"], dt.datetime)
                         else r["fee_date"]) >= since]
        if received and sum(received) > 0:
            self.counts["fee_from_fds"] += 1
            return round(sum(received), 2), "Ongoing"
        return None, None

    def _asset_list(self, hh: Household, source: dict, name_col: str) -> list[list]:
        out = []
        for r in source.get(hh.record_entity.id, []):
            if (clean_text(r.get("status")) or "").lower() in ("recommended",):
                continue
            value = clean_number(r.get("amount"))
            name = clean_text(first_value(r.get(name_col), r.get("type"))) or "Asset"
            owner = owner_pk(r.get("owner"), hh.record_entity, hh) or \
                (clean_text(r.get("owner")) or "")
            if value is not None:
                out.append([name, owner, value])
        return out

    def _assets_ff(self, ff: FactFind, hh: Household) -> None:
        assets = [r for r in self.d.assets.get(hh.record_entity.id, [])
                  if (clean_text(r.get("status")) or "").lower() != "recommended"]
        liabs = self.d.liabs.get(hh.record_entity.id, [])
        typ = lambda r: (clean_text(r.get("type")) or "").lower()
        group = lambda r: (clean_text(r.get("type_group")) or "").lower()
        own = lambda r: owner_pk(r.get("owner"), hh.record_entity, hh)
        homes = [r for r in assets if typ(r) == "primary residence"]
        if homes:
            ff.set("ownsHome", "Yes")
            ff.set("homeValue", sum(clean_number(r["amount"]) or 0 for r in homes),
                   owner=own(homes[0]) if len(homes) == 1 else None)
            debt = sum(clean_number(r["amount"]) or 0 for r in liabs
                       if typ(r) == "primary residence mortgage")
            if debt:
                ff.set("homeDebt", debt)
        liquid = [r for r in assets if group(r) == "liquid assets" or typ(r) == "stocks"]
        if liquid:
            ff.set("hasLiquid", "Yes")
            ff.set("liquidAmount", sum(clean_number(r["amount"]) or 0 for r in liquid))
        props = [r for r in assets if typ(r) == "investment property"]
        for i, r in enumerate(props[:5]):
            ff.set("propName", clean_text(r.get("desc")) or f"Investment property {i + 1}",
                   group="property", index=i, owner=own(r))
            ff.set("propValue", r["amount"], group="property", index=i)
        if len(props) > 5:
            self._problem("entity_assets", "investment property", "property#5+",
                          "more than 5 investment properties")
        others = sorted((r for r in assets if group(r) in ("life style", "investments",
                                                         "business assets")
                         and typ(r) not in ("investment property", "stocks")
                         and (clean_number(r["amount"]) or 0) >= 5000),
                        key=lambda r: -(clean_number(r["amount"]) or 0))
        for i, r in enumerate(others[:5]):
            ff.set("assetName", clean_text(r.get("desc")) or clean_text(r.get("type")),
                   group="otherAsset", index=i, owner=own(r))
            ff.set("assetValue", r["amount"], group="otherAsset", index=i)
        debts = sorted((r for r in liabs if "mortgage" not in typ(r)
                        and (clean_number(r["amount"]) or 0) >= 5000),
                       key=lambda r: -(clean_number(r["amount"]) or 0))
        for i, r in enumerate(debts[:5]):
            ff.set("debtName", clean_text(r.get("account")) or clean_text(r.get("type")),
                   group="otherDebt", index=i, owner=own(r))
            ff.set("debtValue", r["amount"], group="otherDebt", index=i)

    def _funds(self, ff: FactFind, hh: Household) -> None:
        """Super funds and pensions -> accounts, and the first 5 funds -> fact find."""
        rec, n = hh.record_entity, 0
        funds = [r for p in hh.people for r in self.d.funds.get(p.id, [])
                 if (clean_text(r["fund_status"]) or "").lower() != "cancelled"]
        funds.sort(key=lambda r: -(clean_number(r["fund_total_balance"]) or 0))
        for r in funds:
            pk = owner_pk(r["owner_type"], rec, hh) or "1"
            owner = hh.people[int(pk) - 1].name if pk in ("1", "2") else ""
            cover = [f"{label} ${clean_number(r[col]):,.0f}" for col, label in
                     (("life_cover", "Life"), ("tpd_cover", "TPD"),
                      ("salary_continuance", "IP"))
                     if (clean_number(r[col]) or 0) > 0]
            hh.accounts.append({
                "id": f"A{len(hh.accounts) + 1}",
                "p": clean_text(first_value(r["fund_name"], r["supplier"])),
                "prod": clean_text(r["fund_super_plan"]) or "",
                "owner": owner, "kind": "Super",
                "bal": clean_number(r["fund_total_balance"]),
                "asAt": clean_date(r["date_of_balance"]),
                "member": clean_text(r["fund_refnum"]) or "",
                "ins": ", ".join(cover),
            })
            if n < 5:
                ff.set_repeat_person("fund", n, int(pk) if pk in ("1", "2") else 1)
                ff.set("superName", r["fund_name"], group="fund", index=n)
                ff.set("memberNo", r["fund_refnum"], group="fund", index=n)
                ff.set("balance", r["fund_total_balance"], group="fund", index=n)
                if (ft := clean_text(r["fund_type"])):
                    ff.set("fundType", ft, group="fund", index=n, source="ufield_entity_fund.fund_type")
                ff.set("taxFree", money(r["fund_tax_free"]), group="fund", index=n)
                ff.set("untaxed", money(r["fund_taxable_untaxed"]), group="fund", index=n)
                life, tpd = money(r["life_cover"]), money(r["tpd_cover"])
                ip = money(r["salary_continuance"])
                ff.set("insuredInFund", "Yes" if (life or tpd or ip) else "No",
                       group="fund", index=n)
                ff.set("lifeCover", life, group="fund", index=n)
                ff.set("tpdCover", tpd, group="fund", index=n)
                n += 1
        if len(funds) > 5:
            self._problem("ufield_entity_fund", "(6th fund onwards)", "fund#5+",
                          "more than 5 super funds: extra funds are in accounts only")
        for p in hh.people:
            for r in self.d.pensions.get(p.id, []):
                hh.accounts.append({
                    "id": f"A{len(hh.accounts) + 1}",
                    "p": clean_text(first_value(r["provider_name"], r["provider"])),
                    "prod": clean_text(first_value(r["type"], r["desc"])) or "",
                    "owner": p.name, "kind": "Pension",
                    "bal": clean_number(r["pension_balance"]), "asAt": None,
                    "member": clean_text(r["account_num"]) or "", "ins": "",
                })

    def add_platform_accounts(self, hh: Household, owner_ids: list[int],
                              owner_label: str | None = None) -> list[str]:
        """Platform accounts owned by the given entities; returns the new account ids."""
        new = []
        for eid in owner_ids:
            for r in self.d.platform.get(eid, []):
                tax = (clean_text(r["tax_type"]) or "").lower()
                structure = (clean_text(r["taxStructure"]) or "").lower()
                if "pension" in structure:
                    kind = "Pension"
                elif "super" in structure or r.get("subfund_is_super") in (1, True):
                    kind = "Super"
                elif "superfund" in tax:
                    kind = "SMSF"
                else:
                    kind = "Investment"
                owners = r["owners"]
                if owner_label:
                    owner = owner_label
                elif len(owners) > 1:
                    owner = "Joint"
                else:
                    ent = self.d.entities.get(owners[0]) if owners else None
                    owner = ent.name if ent else ""
                acc_id = f"A{len(hh.accounts) + 1}"
                hh.accounts.append({
                    "id": acc_id,
                    "p": clean_text(first_value(r["externalvendorname"], r["linkCode"],
                                                r["subfund"])),
                    "prod": clean_text(r["description"]) or "",
                    "owner": owner, "kind": kind,
                    "bal": r["balance"], "asAt": None,
                    "member": clean_text(first_value(r["externalaccount"],
                                                     r["subfund_account_name"])) or "",
                    "ins": "",
                })
                new.append(acc_id)
        return new

    def _smsf_ff(self, ff: FactFind, hh: Household) -> None:
        funds = self.smsf_for.get(hh.id, [])
        if not funds:
            return
        f = funds[0]
        ff.set("smsfExisting", "Yes")
        ff.set("smsfName", f["name"])
        ff.set("smsfAbn", f.get("abn"))
        ff.set("smsfTfnHeld", f.get("tfnHeld"))
        ff.set("trusteeType", "Corporate" if f.get("corp_acn") is not None else "Individual")
        ff.set("trusteeAcn", f.get("corp_acn") or None)
        if len(funds) > 1:
            self._problem("ufield_entity", "(second SMSF)", "smsf*",
                          "household has more than one SMSF: only the first is in the fact find")

    def _dependants_ff(self, ff: FactFind, hh: Household) -> None:
        deps = [r for p in hh.people for r in self.d.dependants.get(p.id, [])]
        if not deps:
            return
        ff.set("hasDependants", "Yes")
        for i, r in enumerate(deps[:5]):
            ff.set("depWhy", r.get("dep_rel"), group="dependant", index=i)
            ff.set("depAge", r.get("dep_age"), group="dependant", index=i)
            ff.set("depUntil", r.get("dep_until_age"), group="dependant", index=i)
        if len(deps) > 5:
            self._problem("ufield_entity_dependent", "(6th dependant onwards)", "dependant#5+",
                          "more than 5 dependants")

    # -- entities -------------------------------------------------------------
    def entity_records(self) -> list[dict]:
        d = self.d
        structure_ids = {eid for eid in d.clients | set(d.roles)
                         if (e := d.entities.get(eid)) and e.type in STRUCTURE_TYPES}
        out = []
        for eid in sorted(structure_ids):
            e = d.entities[eid]
            roles: dict[tuple[str, str], dict] = {}
            ff: dict[str, Any] = {}
            corp = None
            for person_id, role, row in d.roles.get(eid, []):
                pe = d.entities.get(person_id)
                if role == "Trustee" and pe and pe.type == "company":
                    corp = pe
                    for director_id, r2, _ in d.roles.get(pe.id, []):
                        if r2 == "Director":
                            self._add_role(roles, director_id, "Director")
                    continue
                if role == "Director" and e.type == "company" and e.name not in \
                        self.directorships[person_id]:
                    self.directorships[person_id].append(e.name)
                bal = clean_number(row.get("fund_balance")) if role == "Member" else None
                self._add_role(roles, person_id, role, bal)
            if not roles:
                # Fallback: Xplan's general relationship list (Trustee, Director, Super ...)
                for other, label in d.relations.get(eid, []):
                    if other in self.person_home:
                        role = RELATION_ROLES.get(label.lower(), "Associated")
                        if role == "Associated":
                            self.counts["relationship_role_unclear"] += 1
                        self._add_role(roles, other, role)
                if roles:
                    self.counts["entities_linked_by_relationship"] += 1
            if not roles:
                self.counts["entities_without_household_roles"] += 1
                continue
            role_list = sorted(roles.values(), key=lambda r: (r["c"], r["pk"]))
            first = next((r for r in role_list if {"Trustee", "Member", "Director"}
                          & set(r["roles"])), role_list[0])
            home = self.households[first["c"]]
            label = {"superfund": " (SMSF)", "trust": " (Trust)", "company": " (Company)"}
            accts = self.add_platform_accounts(home, [eid], f"{e.name}{label[e.type]}")
            etype = STRUCTURE_TYPES[e.type]
            if etype == "Trust":
                ttype = (clean_text(e.f.get("trust_type")) or "").lower()
                etype = "Unit trust" if "unit" in ttype else "Family trust"
                if "unit" not in ttype and "family" not in ttype and "discretionary" not in ttype:
                    self.counts["trust_type_assumed_family"] += 1
            if (abn := self._abn(e)):
                ff["abn"] = abn
            tfn = e.f.get("tfn_supplied")
            held = self.d.tfn_held.get(eid) or (
                bool(re.search(r"suppl|provid|held|yes", str(tfn or ""), re.I))
                and not re.search(r"\bnot\b|\bno\b", str(tfn or ""), re.I))
            if held or tfn is not None:
                ff["tfnHeld"] = "Yes" if held else "No"
            if etype == "SMSF":
                ff["trusteeType"] = "Corporate trustee" if corp else "Individual trustees"
            if corp:
                ff["corpTrustee"] = corp.name
            if etype == "Company" and clean_text(e.f.get("company_number")):
                ff["acn"] = clean_text(e.f.get("company_number"))
            if clean_text(e.f.get("accountant")):
                ff["accountant"] = clean_text(e.f.get("accountant"))
            risk = self._risk(e.f.get("risk_profile"), "risk_profile")
            if etype == "SMSF":
                self.smsf_for[home.id].append({
                    "name": e.name, "abn": ff.get("abn"), "tfnHeld": ff.get("tfnHeld"),
                    "corp_acn": (clean_text(corp.f.get("company_number")) or "") if corp else None})
            out.append({
                "id": f"E-{eid}", "ext": {"xplan": str(eid)}, "type": etype, "name": e.name,
                "home": home.id, "roles": role_list,
                "primary": f"{first['c']}:{first['pk']}",
                "accts": [{"c": home.id, "id": a} for a in accts],
                "ff": ff,
                "strategy": {"profile": risk, "reviewed": None, "note": ""},
            })
        return out

    def _add_role(self, roles: dict, person_id: int, role: str, bal: Any = None) -> None:
        home = self.person_home.get(person_id)
        if not home:
            self.counts["roles_for_people_without_household"] += 1
            return
        key = home
        entry = roles.setdefault(key, {"c": home[0], "pk": home[1], "roles": [], "bal": None})
        if role not in entry["roles"]:
            entry["roles"].append(role)
        if bal is not None:
            entry["bal"] = bal

    @staticmethod
    def _abn(e: Entity) -> str | None:
        for v in (e.f.get("abn"), e.f.get("superfund_number"), e.f.get("trust_number")):
            digits = re.sub(r"\D", "", str(v or ""))
            if len(digits) == 11:
                return f"{digits[:2]} {digits[2:5]} {digits[5:8]} {digits[8:]}"
        return None

    def _problem(self, table: str, column: str, target: str, reason: str) -> None:
        self.problems.append(Unmapped(table, column, 1, target, reason))


# --------------------------------------------------------------------------
# File notes, advice history, signed documents
# --------------------------------------------------------------------------

SOA = re.compile(r"statement of advice|\bsoa\b", re.I)
ROA = re.compile(r"record of advice|\broa\b", re.I)
ATP = re.compile(r"authority to proceed|\batp\b", re.I)


def build_notes(data: XplanData, builder: BrightlyBuilder, wanted: set[str],
                entity_home: dict[int, str], progress: Progress):
    """Returns per-household: file notes, advice history, signed documents."""
    db = data.db
    cols = ["docid", "date", "created_at", "created_by", "type", "subtype", "subject",
            "related_entities", "mimetype"]
    data._use("sections_workflow_docnote", *cols, "data")
    relations: dict[int, list[int]] = defaultdict(list)
    data._use("relation_docnote", "id", "related_id")
    for r in db.rows("relation_docnote", ["id", "related_id"]):
        if r["id"] is not None and r["related_id"] is not None:
            relations[int(r["id"])].append(int(r["related_id"]))
    notes: dict[str, list[dict]] = defaultdict(list)
    advice: dict[str, list[dict]] = defaultdict(list)
    signed: dict[str, dict] = defaultdict(dict)
    meta = {}
    for r in db.rows("sections_workflow_docnote", cols):
        homes = []
        for eid in relations.get(int(r["related_entities"] or 0), []):
            h = builder.person_home.get(eid, (entity_home.get(eid),))[0]
            if h and h in wanted and h not in homes:
                homes.append(h)
        if homes:
            meta[str(r["docid"]).strip()] = (r, homes)
    progress(f"  {len(meta):,} file notes belong to the exported households")
    cur = db.conn.cursor()
    cur.execute(f"SELECT docid, data FROM {qname('sections_workflow_docnote')}")
    for docid, body in cur:
        key = str(docid).strip()
        if key not in meta:
            continue
        r, homes = meta[key]
        when = first_value(r["date"], r["created_at"])
        typ = clean_text(r["type"]) or ""
        sub = clean_text(r["subtype"]) or ""
        title = clean_text(r["subject"]) or " - ".join(x for x in (typ, sub) if x) or "File note"
        note = {"at": iso_at(when), "by": data.user(r["created_by"]) or "",
                "title": title, "text": html_to_text(body)}
        label = f"{typ} {sub}"
        for h in homes:
            notes[h].append(note)
            if SOA.search(label) or ROA.search(label):
                advice[h].append({"date": clean_date(when),
                                  "document": "Statement of Advice" if SOA.search(label)
                                  else "Record of Advice",
                                  "scope": sub or typ, "summary": clean_text(r["subject"]) or ""})
            if ATP.search(label):
                d = clean_date(when)
                if d and d > (signed[h].get("atp", {}).get("date") or ""):
                    signed[h]["atp"] = {"date": d, "by": data.user(r["created_by"]) or "",
                                        "note": "From Xplan file note"}
    for h in notes:
        notes[h].sort(key=lambda n: n["at"] or "")
        advice[h].sort(key=lambda a: a["date"] or "")
    return notes, advice, signed


def build_tasks(data: XplanData, builder: BrightlyBuilder, wanted: set[str],
                entity_home: dict[int, str]) -> list[dict]:
    cols = ["id", "subject", "taskname", "client", "assignee", "duedate", "status"]
    data._use("sections_workflow_task", *cols)
    out = []
    for r in data.db.rows("sections_workflow_task", cols):
        try:
            client = int(r["client"])
        except (TypeError, ValueError):
            continue
        h = builder.person_home.get(client, (entity_home.get(client),))[0]
        if not h or h not in wanted:
            continue
        status = (clean_text(r["status"]) or "").lower()
        if status == "aborted":
            builder.counts["tasks_aborted_skipped"] += 1
            continue
        out.append({"id": f"T-{r['id']}",
                    "t": clean_text(first_value(r["subject"], r["taskname"])) or "Task",
                    "c": h, "who": data.user(r["assignee"]) or "",
                    "due": clean_date(r["duedate"]), "done": status == "complete"})
    return out


# --------------------------------------------------------------------------
# Unmapped fields
# --------------------------------------------------------------------------

LINK_COLUMNS = {"eidobj", "entity_id", "entityid", "client_id", "clientid", "client",
                "related_id", "related_entities"}
SUGGEST = [
    (r"health|medical|smok|bmi|height|weight|gp_|claim", "ff health fields (sensitive)"),
    (r"insur|policy|cover|premium|benefit", "insurance (profile.accounts.ins / ff insurance)"),
    (r"fund|super|pension|member", "profile.accounts / ff funds"),
    (r"asset|propert|vehicle", "profile.assets / ff assets"),
    (r"liabilit|loan|mortgage|debt", "profile.liabs / ff debts"),
    (r"income|salary|wage|cashflow|expense", "ff income and cash flow"),
    (r"goal|objective", "profile.goals"),
    (r"fee|fds|invoice|revenue", "fee / feeType / signed"),
    (r"review", "lastReview / nextReview"),
    (r"identity|passport|licen", "signed (ID/AML)"),
    (r"address|street|suburb|postcode", "contacts / ff address"),
    (r"phone|email|mobile|contact", "contacts"),
    (r"employ|occupation|job", "ff employment"),
    (r"will|estate|executor|attorney|beneficiar", "ff estate planning"),
    (r"depend|child", "ff dependants"),
    (r"risk", "profile.risk"),
    (r"task|workflow|thread", "task"),
    (r"transaction|position|portfolio|holding|security|pricing", "(no Brightly home: holdings)"),
]


def suggest(table: str, column: str) -> str:
    text = f"{table} {column}"
    for pattern, target in SUGGEST:
        if re.search(pattern, text, re.I):
            return target
    return ""


def unmapped_inventory(data: XplanData, progress: Progress) -> list[Unmapped]:
    """Every column filled in for at least one record that the export doesn't use."""
    db = data.db
    out: list[Unmapped] = []
    tables = sorted(db.tables.values())
    for t in tables:
        cols = db.columns(t)
        low = t.lower()
        linked = bool(LINK_COLUMNS & set(cols)) or low.startswith(("ufield_entity", "sections_",
                                                                    "ips_", "relation_"))
        if not linked or low.startswith("_metadata"):
            continue
        used = data.mapped.get(low, set())
        candidates = []
        for c_low, actual in cols.items():
            if c_low in TECHNICAL or c_low in used or c_low.endswith("_vlu") or "#currency" in c_low:
                continue
            if c_low.endswith("_vlu") or (c_low + "_vlu") in used:
                continue
            if is_tfn_column(c_low) and not c_low.startswith("tfn_supplied"):
                continue  # TFNs are dropped, never listed with values
            candidates.append(actual)
        if not candidates:
            continue
        parts = [f"SUM(CASE WHEN NULLIF(LTRIM(CAST([{c}] AS nvarchar(100))), '') IS NOT NULL "
                 f"AND CAST([{c}] AS nvarchar(100)) NOT IN ('0', '0.0', 'False', '[]', '-1') "
                 f"THEN 1 ELSE 0 END)" for c in candidates]
        types = {r[0]: r[1] for r in db.conn.cursor().execute(
            "SELECT c.name, ty.name FROM sys.columns c JOIN sys.types ty "
            "ON ty.user_type_id = c.user_type_id WHERE c.object_id = OBJECT_ID(?)", qname(t))}
        ok = [c for c in candidates if types.get(c) not in ("varbinary", "binary", "image",
                                                          "geography", "geometry", "xml",
                                                          "timestamp", "hierarchyid",
                                                          "sql_variant")]
        if not ok:
            continue
        parts = []
        for c in ok:
            v = f"CAST([{c}] AS nvarchar(100))"
            parts.append(f"SUM(CASE WHEN NULLIF(LTRIM({v}), '') IS NOT NULL AND {v} NOT IN "
                         f"('0', '0.0', 'False', '[]', '-1') THEN 1 ELSE 0 END)")
            parts.append(f"COUNT(DISTINCT {v})")
        try:
            row = db.conn.cursor().execute(
                f"SELECT COUNT(*), {', '.join(parts)} FROM {qname(t)}").fetchone()
        except Exception as exc:
            progress(f"  (skipped {t}: {str(exc).splitlines()[0][:80]})")
            continue
        total = row[0]
        for i, c in enumerate(ok):
            n, distinct = row[1 + 2 * i], row[2 + 2 * i]
            if not n:
                continue
            if distinct == 1 and n == total and total > 1:
                continue  # the same default on every record: nobody filled it in
            out.append(Unmapped(t, c, int(n), suggest(t, c), "not mapped"))
    return out
