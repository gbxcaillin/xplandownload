import json

import pytest
import sqlalchemy as sa

from xplan_extract.brightly import database as db
from xplan_extract.brightly import loader

# Fictional records only - no real client data in tests.
HOUSEHOLD = {
    "id": "H-101", "ext": {"xplan": "101"}, "name": "Alex & Sam Sample", "type": "Couple",
    "adviser": "Pat Adviser", "since": "2019-03-01", "email": "alex@example.com",
    "phone": "0400 000 000", "city": "Testville", "notes": "", "lastReview": "2026-04-14",
    "nextReview": "2027-04-14", "ofa": "2027-03-01", "insRenewal": None, "fee": 3300,
    "feeType": "Ongoing", "status": "Active",
    "profile": {
        "people": [{"n": "Alex Sample", "role": "Client", "dob": "1964-05-02", "job": "Teacher",
                    "income": 95000},
                   {"n": "Sam Sample", "role": "Partner", "dob": "1962-11-20", "job": None,
                    "income": None}],
        "accounts": [{"id": "A1", "p": "Sample Super", "prod": "Balanced", "owner": "Alex Sample",
                      "kind": "Super", "bal": 412000, "asAt": "2026-09-30", "member": "M1",
                      "ins": ""},
                     {"id": "A2", "p": "Sample Wrap", "prod": "Growth", "owner": "Sample SMSF",
                      "kind": "SMSF", "bal": 655000, "asAt": "2026-09-30", "member": "W2",
                      "ins": ""}],
        "assets": [["Home", "joint", 1250000]], "liabs": [["Home loan", "joint", 180000]],
        "goals": ["Retire at 67"], "goalMeta": [{"date": "2026-04-14", "source": "Xplan"}],
        "risk": "Balanced",
        "adviceHist": [{"date": "2024-07-18", "document": "Statement of Advice",
                        "scope": "Super", "summary": "Consolidate"}]},
    "contacts": {"emails": [{"addr": "alex@example.com", "primary": True}],
                 "phones": [{"num": "0400 000 000", "primary": True}]},
    "fileNotes": [{"at": "2026-04-14T10:30:00+10:00", "by": "Pat Adviser", "title": "Review",
                   "text": "Reviewed goals."}],
    "ff": {"a": {"marital": "Married", "dob@1": "1964-05-02"}, "n": {}},
    "signed": {"atp": {"date": "2024-07-25", "by": "Pat Adviser"}},
    "log": [{"at": "2026-10-06T09:00:00+10:00", "by": "Xplan import", "kind": "Imported",
             "m": "Imported from Xplan client 101"}],
}
PROSPECT = {"id": "H-202", "ext": {"xplan": "202"}, "name": "Jo Lead", "type": "Individual",
            "prospect": True, "source": "Referral", "added": "2026-01-02", "status": "Inactive",
            "profile": {"people": [{"n": "Jo Lead", "role": "Client"}]}}
ENTITY = {"id": "E-301", "ext": {"xplan": "301"}, "type": "SMSF", "name": "Sample SMSF",
          "home": "H-101", "status": "Active",
          "roles": [{"c": "H-101", "pk": "1", "roles": ["Trustee", "Member"], "bal": 400000},
                    {"c": "H-101", "pk": "2", "roles": ["Trustee", "Member"], "bal": 255000}],
          "primary": "H-101:1", "accts": [{"c": "H-101", "id": "A2"}],
          "ff": {"abn": "12 345 678 901", "tfnHeld": "Yes", "auditor": "Sample Audit"},
          "strategy": {"profile": "Balanced", "reviewed": "2026-03-01", "note": ""}}
TASK = {"id": "T-401", "t": "Book review", "c": "H-101", "who": "Pat Adviser",
        "due": "2027-03-14", "done": False}


def write_export(path, family_groups=(HOUSEHOLD,), prospects=(PROSPECT,), entities=(ENTITY,),
                 tasks=(TASK,), counts=None):
    path.mkdir(parents=True, exist_ok=True)
    data = {"family_groups.jsonl": family_groups, "prospects.jsonl": prospects,
            "entities.jsonl": entities, "tasks.jsonl": tasks}
    for name, rows in data.items():
        (path / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (path / "manifest.json").write_text(json.dumps({
        "exported_at": "2026-10-08T10:00:00+11:00", "xplan_source": "test",
        "record_counts": counts or {k: len(v) for k, v in data.items()}}))
    return path


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    sa.event.listen(e, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    db.create_all(e)
    return e


def test_load_and_reload_keeps_brightly_rows(engine, tmp_path):
    exp = write_export(tmp_path / "export")
    res = loader.load_export(engine, exp, progress=lambda m: None)
    assert res.status == "loaded", res.problems
    counts = loader.table_counts(engine)
    assert counts["family_group"] == 2 and counts["person"] == 3 and counts["account"] == 2
    assert counts["entity_role"] == 2 and counts["task"] == 1 and counts["file_note"] == 1
    with engine.connect() as c:
        assert c.execute(sa.select(db.account.c.entity_id).where(
            db.account.c.id == "H-101:A2")).scalar_one() == "E-301"
        ent = c.execute(sa.select(db.entity)).mappings().one()
        assert ent["tfn_held"] == "Yes" and ent["abn"] == "12 345 678 901"
        assert c.execute(sa.select(db.family_group.c.status).where(
            db.family_group.c.id == "H-202")).scalar_one() == "Inactive"

    # a note added in Brightly after go-live must survive a re-import
    with engine.begin() as c:
        c.execute(db.file_note.insert().values(family_group_id="H-101", title="New",
                                               text="Added in Brightly", source="brightly"))
    res2 = loader.load_export(engine, exp, progress=lambda m: None)
    assert res2.status == "loaded"
    with engine.connect() as c:
        sources = sorted(r[0] for r in c.execute(sa.select(db.file_note.c.source)))
    assert sources == ["brightly", "xplan"]
    assert loader.table_counts(engine)["family_group"] == 2  # updated, not duplicated


def test_edited_record_is_left_alone(engine, tmp_path):
    import datetime as dt
    exp = write_export(tmp_path / "export")
    loader.load_export(engine, exp, progress=lambda m: None)
    with engine.begin() as c:
        c.execute(db.family_group.update().where(db.family_group.c.id == "H-101").values(
            name="Renamed in Brightly",
            updated_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)))
    res = loader.load_export(engine, exp, progress=lambda m: None)
    assert any("edited in Brightly" in w for w in res.warnings)
    with engine.connect() as c:
        assert c.execute(sa.select(db.family_group.c.name).where(
            db.family_group.c.id == "H-101")).scalar_one() == "Renamed in Brightly"


def test_validation_blocks_tfn_and_bad_counts(engine, tmp_path):
    bad = dict(HOUSEHOLD, notes="TFN 123 456 782")   # passes the ATO check digit
    exp = write_export(tmp_path / "export", family_groups=(bad,),
                       counts={"family_groups.jsonl": 5})
    res = loader.load_export(engine, exp, progress=lambda m: None)
    assert res.status == "failed"
    text = " ".join(res.problems)
    assert "TFN" in text and "manifest says 5" in text
    assert "Sample" not in text  # problems name ids, never client names
    assert loader.table_counts(engine)["family_group"] == 0


def test_dry_run_loads_nothing(engine, tmp_path):
    res = loader.load_export(engine, write_export(tmp_path / "e"), dry_run=True,
                             progress=lambda m: None)
    assert res.status == "dry-run" and loader.table_counts(engine)["family_group"] == 0


def test_ddl_for_both_databases():
    pg, ms = db.ddl("postgresql"), db.ddl("mssql")
    assert "JSONB" in pg and "append-only" in pg
    assert "NVARCHAR(max)" in ms and "CREATE TABLE family_group" in ms


def test_old_export_name_still_loads(engine, tmp_path):
    exp = write_export(tmp_path / "old")
    (exp / "family_groups.jsonl").rename(exp / "households.jsonl")
    m = json.loads((exp / "manifest.json").read_text())
    m["record_counts"]["households.jsonl"] = m["record_counts"].pop("family_groups.jsonl")
    (exp / "manifest.json").write_text(json.dumps(m))
    res = loader.load_export(engine, exp, progress=lambda m: None)
    assert res.status == "loaded" and loader.table_counts(engine)["family_group"] == 2


def test_staged_roll_in_active_then_all(engine, tmp_path):
    """Phase 1 loads only the active family group; phase 2 (everything) loads on top."""
    from collections import Counter

    from xplan_extract.brightly.export import links_within

    inactive = {**HOUSEHOLD, "id": "H-102", "ext": {"xplan": "102"}, "name": "Old Client",
                "status": "Inactive"}
    shared = {**ENTITY, "home": "H-102",
              "roles": ENTITY["roles"] + [{"c": "H-102", "pk": "1", "roles": ["Member"],
                                           "bal": 1}],
              "primary": "H-102:1", "accts": ENTITY["accts"] + [{"c": "H-102", "id": "A9"}]}
    counts = Counter()
    phase1 = links_within(shared, {"H-101"}, counts)
    assert phase1["home"] == "H-101" and counts["entity_home_moved_for_phase"] == 1
    assert {r["c"] for r in phase1["roles"]} == {"H-101"} and phase1["primary"] is None
    assert shared["home"] == "H-102"                      # the original is untouched

    loader.load_export(engine, write_export(tmp_path / "p1", family_groups=(HOUSEHOLD,),
                                            prospects=(), entities=(phase1,)),
                       progress=lambda m: None)
    with engine.connect() as c:
        assert c.execute(sa.text("select count(*) from family_group")).scalar() == 1
        roles1 = c.execute(sa.text("select count(*) from entity_role")).scalar()

    loader.load_export(engine, write_export(tmp_path / "p2", family_groups=(HOUSEHOLD, inactive),
                                            entities=(shared,)),
                       progress=lambda m: None)
    with engine.connect() as c:
        assert c.execute(sa.text("select count(*) from family_group")).scalar() == 3  # + prospect
        assert c.execute(sa.text("select count(*) from entity")).scalar() == 1
        assert c.execute(sa.text("select count(*) from entity_role")).scalar() > roles1
        home = c.execute(sa.text("select home_family_group_id from entity")).scalar()
    assert home == "H-102"
