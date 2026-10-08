"""Draft Brightly database schema (step 3.1).

Relational core for the fixed parts of the record model, JSON columns for the flexible parts
(the fact find, entity details), as the Brightly brief sketches. Written with SQLAlchemy Core
so the same definition creates PostgreSQL, Azure SQL / SQL Server or SQLite tables: Scott's
database choice doesn't change the loader.

Design notes
- Ids are Brightly's own (`H-…`, `E-…`, `T-…`); `xplan_id` keeps the Xplan id so a re-run
  updates instead of duplicating.
- Child rows carry `source` ('xplan' or 'brightly'). A re-import replaces only the 'xplan'
  rows, so file notes, contacts and so on added in Brightly after go-live survive it.
- `imported_at` / `updated_at` on family groups and entities: the app sets `updated_at` on every
  edit, and the loader won't overwrite a record edited in Brightly since its last import
  unless told to (it reports it instead).
- No tax file numbers anywhere: `tfn_held` is Yes/No only. Health answers live only inside
  `ff_answers`.
- "Family group" is the industry term for what Brightly's export calls a household; the table
  is `family_group`. Duplicate family groups or entities are merged with merge.py, and
  `merge_record` remembers each merge so a later re-import doesn't bring the duplicate back.
- `change_log` is append-only (the PostgreSQL DDL adds a trigger that refuses UPDATE/DELETE).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql

metadata = sa.MetaData()

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
Money = sa.Numeric(14, 2)
Stamp = sa.DateTime(timezone=True)


def _id(name: str = "id") -> sa.Column:
    return sa.Column(name, sa.String(64), primary_key=True)


def _auto() -> sa.Column:
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                     primary_key=True, autoincrement=True)


def _source() -> sa.Column:
    return sa.Column("source", sa.String(16), nullable=False, server_default="xplan")


import_run = sa.Table(
    "import_run", metadata,
    _auto(),
    sa.Column("started_at", Stamp, nullable=False),
    sa.Column("finished_at", Stamp),
    sa.Column("status", sa.String(16), nullable=False),  # staged | failed | loaded | dry-run
    sa.Column("source", sa.String(200)),
    sa.Column("export_exported_at", sa.String(40)),
    sa.Column("manifest", JSONType),
    sa.Column("counts", JSONType),
    sa.Column("problems", JSONType),
)

staging_record = sa.Table(
    "staging_record", metadata,
    sa.Column("run_id", sa.ForeignKey("import_run.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("kind", sa.String(16), primary_key=True),  # household (family group) | prospect | entity | task
    sa.Column("line_no", sa.Integer, primary_key=True),
    sa.Column("record_id", sa.String(64)),
    sa.Column("payload", sa.Text().with_variant(mssql.NVARCHAR(None), "mssql"), nullable=False),
)

family_group = sa.Table(
    "family_group", metadata,
    _id(),
    sa.Column("xplan_id", sa.String(32), unique=True),
    sa.Column("is_prospect", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(16)),            # Active | Inactive (hidden unless searched)
    sa.Column("name", sa.String(300), nullable=False),
    sa.Column("type", sa.String(16)),              # Individual | Couple
    sa.Column("adviser", sa.String(200)),
    sa.Column("since", sa.Date),
    sa.Column("email", sa.String(320)),
    sa.Column("phone", sa.String(64)),
    sa.Column("city", sa.String(120)),
    sa.Column("notes", sa.Text),
    sa.Column("last_review", sa.Date),
    sa.Column("next_review", sa.Date),
    sa.Column("ofa", sa.Date),
    sa.Column("ins_renewal", sa.Date),
    sa.Column("fee", Money),
    sa.Column("fee_type", sa.String(32)),
    sa.Column("risk", sa.String(32)),
    sa.Column("prospect_source", sa.String(200)),
    sa.Column("prospect_added", sa.Date),
    sa.Column("ff_answers", JSONType),             # fact find answers (ff.a), incl. health fields
    sa.Column("ff_counts", JSONType),              # repeat-group counts (ff.n)
    sa.Column("sharepoint_folder", sa.String(1000)),
    sa.Column("imported_at", Stamp),
    sa.Column("updated_at", Stamp),
    sa.Column("import_run_id", sa.ForeignKey("import_run.id")),
    sa.Index("ix_family_group_status", "status"),
    sa.Index("ix_family_group_name", "name"),
)

person = sa.Table(
    "person", metadata,
    _id(),                                          # "<family group id>:1" / ":2"
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("position", sa.SmallInteger, nullable=False),
    sa.Column("name", sa.String(300)),
    sa.Column("role", sa.String(16)),               # Client | Partner
    sa.Column("dob", sa.Date),
    sa.Column("occupation", sa.String(200)),
    sa.Column("income", Money),
    sa.UniqueConstraint("family_group_id", "position"),
)

contact_point = sa.Table(
    "contact_point", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("kind", sa.String(8), nullable=False),  # email | phone
    sa.Column("value", sa.String(320), nullable=False),
    sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
    _source(),
    sa.Index("ix_contact_family_group", "family_group_id"),
)

entity = sa.Table(
    "entity", metadata,
    _id(),
    sa.Column("xplan_id", sa.String(32), unique=True),
    sa.Column("type", sa.String(32), nullable=False),  # SMSF | Family trust | Unit trust | Company
    sa.Column("name", sa.String(300), nullable=False),
    sa.Column("status", sa.String(16)),
    sa.Column("home_family_group_id", sa.ForeignKey("family_group.id")),
    sa.Column("primary_ref", sa.String(80)),        # "<family group id>:<1|2>"
    sa.Column("abn", sa.String(20)),
    sa.Column("tfn_held", sa.String(3)),             # Yes | No - never the number
    sa.Column("details", JSONType),                  # the rest of entity.ff
    sa.Column("strategy_profile", sa.String(32)),
    sa.Column("strategy_reviewed", sa.Date),
    sa.Column("strategy_note", sa.Text),
    sa.Column("imported_at", Stamp),
    sa.Column("updated_at", Stamp),
    sa.Column("import_run_id", sa.ForeignKey("import_run.id")),
)

account = sa.Table(
    "account", metadata,
    _id(),                                           # "<family group id>:<account id>"
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("source_ref", sa.String(64)),          # the account id inside the record
    sa.Column("platform", sa.String(300)),
    sa.Column("product", sa.String(300)),
    sa.Column("kind", sa.String(32)),
    sa.Column("owner", sa.String(300)),
    sa.Column("balance", Money),
    sa.Column("as_at", sa.Date),
    sa.Column("member_no", sa.String(100)),
    sa.Column("insurance", sa.Text),
    sa.Column("entity_id", sa.ForeignKey("entity.id", ondelete="SET NULL")),
    _source(),
    sa.Index("ix_account_family_group", "family_group_id"),
)

asset_liability = sa.Table(
    "asset_liability", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("kind", sa.String(10), nullable=False),  # asset | liability
    sa.Column("name", sa.String(300)),
    sa.Column("owner", sa.String(8)),                   # 1 | 2 | joint
    sa.Column("value", Money),
    _source(),
    sa.Index("ix_assetliab_family_group", "family_group_id"),
)

goal = sa.Table(
    "goal", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("position", sa.SmallInteger),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("date", sa.Date),
    sa.Column("origin", sa.String(64)),
    _source(),
    sa.Index("ix_goal_family_group", "family_group_id"),
)

advice_history = sa.Table(
    "advice_history", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("date", sa.Date),
    sa.Column("document", sa.String(64)),            # Statement of Advice | Record of Advice
    sa.Column("scope", sa.Text),
    sa.Column("summary", sa.Text),
    _source(),
    sa.Index("ix_advice_family_group", "family_group_id"),
)

file_note = sa.Table(
    "file_note", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE"), nullable=False),
    sa.Column("at", Stamp),
    sa.Column("author", sa.String(200)),
    sa.Column("title", sa.String(500)),
    sa.Column("text", sa.Text().with_variant(mssql.NVARCHAR(None), "mssql")),
    _source(),
    sa.Index("ix_note_family_group_at", "family_group_id", "at"),
)

signed_document = sa.Table(
    "signed_document", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE")),
    sa.Column("entity_id", sa.ForeignKey("entity.id", ondelete="CASCADE")),
    sa.Column("doc_key", sa.String(64), nullable=False),  # atp | ofa | fee consent | id ...
    sa.Column("date", sa.Date),
    sa.Column("signed_by", sa.String(200)),
    sa.Column("note", sa.Text),
    _source(),
)

entity_role = sa.Table(
    "entity_role", metadata,
    _auto(),
    sa.Column("entity_id", sa.ForeignKey("entity.id", ondelete="CASCADE"), nullable=False),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id"), nullable=False),
    sa.Column("person_pos", sa.String(2)),           # "1" | "2"
    sa.Column("person_id", sa.ForeignKey("person.id")),
    sa.Column("roles", JSONType),                     # ["Trustee", "Member", ...]
    sa.Column("member_balance", Money),
    sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
    _source(),
    sa.Index("ix_role_entity", "entity_id"),
    sa.Index("ix_role_family_group", "family_group_id"),
)

task = sa.Table(
    "task", metadata,
    _id(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE")),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("who", sa.String(200)),
    sa.Column("due", sa.Date),
    sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(16)),
    _source(),
    sa.Column("imported_at", Stamp),
    sa.Column("updated_at", Stamp),
    sa.Index("ix_task_family_group", "family_group_id"),
    sa.Index("ix_task_due", "due"),
)

document_link = sa.Table(
    "document_link", metadata,
    _auto(),
    sa.Column("family_group_id", sa.ForeignKey("family_group.id", ondelete="CASCADE")),
    sa.Column("entity_id", sa.ForeignKey("entity.id", ondelete="CASCADE")),
    sa.Column("sharepoint_path", sa.String(1000), nullable=False),  # files stay in SharePoint
    sa.Column("title", sa.String(500)),
    sa.Column("kind", sa.String(64)),
    sa.Column("doc_date", sa.Date),
    _source(),
)

change_log = sa.Table(
    "change_log", metadata,
    _auto(),
    sa.Column("at", Stamp, nullable=False),
    sa.Column("actor", sa.String(200)),
    sa.Column("record_type", sa.String(16), nullable=False),
    sa.Column("record_id", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(32)),
    sa.Column("message", sa.Text),
    sa.Index("ix_log_record", "record_type", "record_id"),
)

merge_record = sa.Table(
    "merge_record", metadata,
    _auto(),
    sa.Column("kind", sa.String(16), nullable=False),          # family_group | entity
    sa.Column("kept_id", sa.String(64), nullable=False),
    sa.Column("dropped_id", sa.String(64), nullable=False, unique=True),
    sa.Column("dropped_xplan_id", sa.String(32)),
    sa.Column("at", Stamp, nullable=False),
    sa.Column("actor", sa.String(200)),
    sa.Column("choices", JSONType),                             # field -> which record won / excluded
    sa.Column("snapshot", JSONType),                            # the dropped record as it was
    sa.Index("ix_merge_kept", "kept_id"),
)

# Tables a re-import rebuilds for a family group (only their source='xplan' rows).
FAMILY_GROUP_CHILDREN = [contact_point, account, asset_liability, goal, advice_history, file_note,
                      signed_document]

APPEND_ONLY_PG = """
CREATE OR REPLACE FUNCTION change_log_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'change_log is append-only'; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER change_log_no_update BEFORE UPDATE OR DELETE ON change_log
  FOR EACH ROW EXECUTE FUNCTION change_log_append_only();
"""

APPEND_ONLY_MSSQL = """
CREATE TRIGGER change_log_no_update ON change_log INSTEAD OF UPDATE, DELETE AS
BEGIN THROW 50001, 'change_log is append-only', 1; END;
"""


def ddl(dialect: str) -> str:
    """CREATE statements for 'postgresql', 'mssql' or 'sqlite', for review before running."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    d = {"postgresql": postgresql.dialect(), "mssql": mssql.dialect()}.get(dialect)
    if d is None:
        from sqlalchemy.dialects import sqlite
        d = sqlite.dialect()
    out = [f"-- Brightly draft schema ({dialect}). Generated by: python -m xplan_extract "
           f"schema-sql --dialect {dialect}\n-- Review before running; nothing has been created."]
    for t in metadata.sorted_tables:
        out.append(str(CreateTable(t).compile(dialect=d)).strip() + ";")
        for ix in sorted(t.indexes, key=lambda i: i.name):
            out.append(str(CreateIndex(ix).compile(dialect=d)).strip() + ";")
    if dialect == "postgresql":
        out.append(APPEND_ONLY_PG.strip())
    elif dialect == "mssql":
        out.append("GO\n" + APPEND_ONLY_MSSQL.strip() + "\nGO")
    return "\n\n".join(out) + "\n"


def create_all(engine: sa.Engine) -> None:
    metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as c:
            exists = c.execute(sa.text(
                "SELECT 1 FROM pg_trigger WHERE tgname = 'change_log_no_update'")).first()
            if not exists:
                c.exec_driver_sql(APPEND_ONLY_PG)
    elif engine.dialect.name == "mssql":
        with engine.begin() as c:
            if not c.execute(sa.text("SELECT 1 FROM sys.triggers WHERE name = "
                                     "'change_log_no_update'")).first():
                c.exec_driver_sql(APPEND_ONLY_MSSQL)
