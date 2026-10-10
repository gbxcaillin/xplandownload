"""Change history (PostgreSQL triggers) and locked advice records. The PostgreSQL tests run when
BRIGHTLY_TEST_PG is set to a throwaway database URL. Fictional data only."""

import datetime as dt
import os
import urllib.error

import pytest
import sqlalchemy as sa

from xplan_extract.brightly import audit, database as db, loader, merge, ofa_store, records

from test_brightly_loader import HOUSEHOLD, write_export

PG = os.environ.get("BRIGHTLY_TEST_PG")


class FakeAzure:
    def __init__(self):
        self.puts = {}

    def __call__(self, req, timeout=0):
        name = req.full_url.split("?")[0]
        if name in self.puts:
            raise urllib.error.HTTPError(req.full_url, 409, "BlobAlreadyExists", {}, None)
        self.puts[name] = (dict(req.header_items()), req.data)

        class R:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()


def test_lock_record(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    db.create_all(e)
    f = tmp_path / "SOA Jane Sample.pdf"
    f.write_bytes(b"%PDF-1.7 advice")
    azure = FakeAzure()
    row = records.lock(e, "https://acct.blob.core.windows.net/advice-records?sv=x&sig=y", f,
                       family_group_id="H-101", kind="soa", record_date=dt.date(2026, 10, 10),
                       actor="pat", opener=azure)
    assert row["locked_until"] == dt.date(2033, 10, 10)
    (url, (headers, data)), = azure.puts.items()
    assert url.startswith("https://acct.blob.core.windows.net/advice-records/H-101/2026/")
    h = {k.lower(): v for k, v in headers.items()}
    assert h["x-ms-immutability-policy-mode"] == "Locked"
    assert h["x-ms-immutability-policy-until-date"] == "Mon, 10 Oct 2033 00:00:00 GMT"
    assert h["if-none-match"] == "*" and h["x-ms-meta-sha256"] == row["sha256"]
    assert data == b"%PDF-1.7 advice" and records.verify(f, row["sha256"])
    with pytest.raises(records.RecordError, match="already locked"):
        records.lock(e, "https://a/advice-records?s", f, family_group_id="H-101", kind="soa",
                     record_date=dt.date(2026, 10, 10), actor="pat", opener=azure)
    with pytest.raises(sa.exc.DatabaseError):
        with e.begin() as c:
            c.execute(db.advice_record.delete())
    assert records.locked_until(dt.date(2028, 2, 29)) == dt.date(2035, 2, 28)


@pytest.fixture
def pg(tmp_path):
    if not PG:
        pytest.skip("set BRIGHTLY_TEST_PG to a throwaway PostgreSQL database to run this")
    e = sa.create_engine(PG)
    with e.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.create_all(e)
    loader.load_export(e, write_export(tmp_path / "x"), progress=lambda m: None)
    return e


def test_import_is_quiet_and_edits_are_recorded(pg):
    with pg.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(db.audit_log)).scalar() == 0
    with pg.begin() as c:
        audit.set_actor(c, "pat@example.com.au")
        c.execute(db.family_group.update().where(db.family_group.c.id == HOUSEHOLD["id"])
                  .values(email="new@example.com", updated_at=dt.datetime.now(dt.timezone.utc)))
        c.execute(db.contact_point.insert().values(family_group_id=HOUSEHOLD["id"], kind="phone",
                                                   value="0400 111 222", source="brightly"))
    h = audit.history(pg, HOUSEHOLD["id"])
    upd = [x for x in h if x["op"] == "UPDATE"][0]
    assert upd["actor"] == "pat@example.com.au" and upd["table_name"] == "family_group"
    assert upd["changes"] == {"email": ["alex@example.com", "new@example.com"]}
    with pg.begin() as c:                 # nothing changed: nothing recorded
        c.execute(db.family_group.update().where(db.family_group.c.id == HOUSEHOLD["id"])
                  .values(email="new@example.com"))
    assert len(audit.history(pg, HOUSEHOLD["id"])) == len(h)
    with pytest.raises(sa.exc.DatabaseError):
        with pg.begin() as c:
            c.execute(db.audit_log.delete())
    with pytest.raises(sa.exc.DatabaseError):
        with pg.begin() as c:
            c.execute(db.audit_log.update().values(actor="someone else"))


def test_merge_moves_fee_arrangements(pg, tmp_path):
    ofa_store.seed(pg, "t", today=dt.date(2026, 10, 10))
    dup = {**HOUSEHOLD, "id": "H-102", "ext": {"xplan": "102"}}
    loader.load_export(pg, write_export(tmp_path / "y", family_groups=(HOUSEHOLD, dup),
                                        prospects=(), entities=(), tasks=()),
                       progress=lambda m: None)
    ofa_store.seed(pg, "t", today=dt.date(2026, 10, 10))
    merge.apply_merge(pg, "family_group", HOUSEHOLD["id"], "H-102", actor="pat")
    with pg.connect() as c:
        owners = {r[0] for r in c.execute(sa.select(db.ofa_arrangement.c.family_group_id))}
        events = c.execute(sa.select(sa.func.count()).select_from(db.ofa_event)).scalar()
    assert owners == {HOUSEHOLD["id"]} and events >= 2      # evidence kept, nothing deleted
    assert any(x["actor"] == "pat" and x["op"] == "DELETE"
               for x in audit.history(pg, "H-102"))
