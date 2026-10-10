"""Field-level change history for client records (step 7.2).

On PostgreSQL a trigger on every client table writes who changed what, and when, to
`audit_log`, which nothing can edit or delete. Inserts and deletes keep the whole row; updates keep
only the fields that changed, as [old, new]. The app says who is acting by calling
`set_actor(conn, email)` in each transaction (it becomes the session setting `brightly.actor`);
otherwise the database login is recorded.

Bulk imports switch the trigger off for their own transaction (`quiet(conn)`): the import itself is
recorded once in import_run / change_log rather than as 100,000 row inserts.
"""

from __future__ import annotations

import sqlalchemy as sa

AUDITED = ("family_group", "person", "contact_point", "account", "asset_liability", "goal",
           "advice_history", "file_note", "signed_document", "entity", "entity_role", "task",
           "document_link", "ofa_arrangement", "ofa_account", "ofa_consent")

FUNCTION_PG = """
CREATE OR REPLACE FUNCTION brightly_audit_row() RETURNS trigger AS $$
DECLARE
  old_j jsonb; new_j jsonb; diff jsonb := '{}'::jsonb; k text;
BEGIN
  IF current_setting('brightly.audit', true) = 'off' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  IF TG_OP = 'INSERT' THEN
    new_j := to_jsonb(NEW); diff := new_j;
  ELSIF TG_OP = 'DELETE' THEN
    old_j := to_jsonb(OLD); diff := old_j;
  ELSE
    old_j := to_jsonb(OLD); new_j := to_jsonb(NEW);
    FOR k IN SELECT jsonb_object_keys(new_j) LOOP
      IF k <> 'updated_at' AND (new_j -> k) IS DISTINCT FROM (old_j -> k) THEN
        diff := diff || jsonb_build_object(k, jsonb_build_array(old_j -> k, new_j -> k));
      END IF;
    END LOOP;
    IF diff = '{}'::jsonb THEN RETURN NEW; END IF;
  END IF;
  INSERT INTO audit_log (at, actor, table_name, record_id, op, changes)
  VALUES (now(), COALESCE(NULLIF(current_setting('brightly.actor', true), ''), session_user),
          TG_TABLE_NAME, COALESCE(COALESCE(new_j, old_j) ->> 'id', ''), TG_OP, diff);
  RETURN COALESCE(NEW, OLD);
END $$ LANGUAGE plpgsql;
"""


def trigger_sql(table: str) -> str:
    return (f"DROP TRIGGER IF EXISTS {table}_audit ON {table};\n"
            f"CREATE TRIGGER {table}_audit AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION brightly_audit_row();")


def install(engine: sa.Engine) -> bool:
    """Create the audit function and triggers (PostgreSQL only; safe to run again)."""
    if engine.dialect.name != "postgresql":
        return False
    with engine.begin() as c:
        c.exec_driver_sql(FUNCTION_PG)
        for t in AUDITED:
            c.exec_driver_sql(trigger_sql(t))
    return True


def set_actor(conn, actor: str) -> None:
    """Who is making the changes in this transaction (PostgreSQL)."""
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT set_config('brightly.actor', :a, true)"), {"a": actor[:200]})


def quiet(conn) -> None:
    """No row-level audit for the rest of this transaction (bulk imports)."""
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT set_config('brightly.audit', 'off', true)"))


def history(engine: sa.Engine, record_id: str, limit: int = 200) -> list[dict]:
    """A record's change history, newest first: family group or entity, and anything whose id
    starts with it (its people, accounts ...)."""
    from . import database as db
    t = db.audit_log
    with engine.connect() as c:
        rows = c.execute(sa.select(t).where(sa.or_(t.c.record_id == record_id,
                                                   t.c.record_id.like(f"{record_id}:%")))
                         .order_by(t.c.id.desc()).limit(limit)).mappings()
        return [dict(r) for r in rows]
