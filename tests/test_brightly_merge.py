import copy

import pytest
import sqlalchemy as sa

from test_brightly_loader import ENTITY, HOUSEHOLD, PROSPECT, TASK, write_export
from xplan_extract.brightly import database as db
from xplan_extract.brightly import loader, merge

# A second, fictional copy of the same family entered separately in Xplan.
DUP = copy.deepcopy(HOUSEHOLD)
DUP.update({"id": "H-102", "ext": {"xplan": "102"}, "name": "Sam & Alex Sample",
            "adviser": "Lee Adviser",            # conflict
            "email": "sam@example.com",          # conflict
            "city": None,                         # blank here, filled in H-101
            "insRenewal": "2027-01-31",           # blank in H-101, filled here
            "fee": None, "notes": "Second record"})
DUP["contacts"] = {"emails": [{"addr": "sam@example.com", "primary": True},
                              {"addr": "alex@example.com", "primary": False}], "phones": []}
DUP["fileNotes"] = [{"at": "2025-01-01T09:00:00+10:00", "by": "Pat Adviser", "title": "Old",
                     "text": "Earlier note"}]
DUP["ff"] = {"a": {"marital": "Married", "fund#0.superName": "Other Super", "ownsHome": "Yes"},
             "n": {"fund": 1}}
DUP["profile"]["accounts"] = [{"id": "A1", "p": "Other Super", "kind": "Super", "bal": 1000}]
ENT2 = copy.deepcopy(ENTITY)
ENT2.update({"id": "E-302", "ext": {"xplan": "302"}, "home": "H-102", "primary": "H-102:1",
             "roles": [{"c": "H-102", "pk": "1", "roles": ["Trustee"], "bal": 1}],
             "accts": [], "ff": {"abn": "12 345 678 901", "tfnHeld": "Yes", "accountant": "Acme"}})
TASK2 = dict(TASK, id="T-402", c="H-102")


@pytest.fixture
def loaded(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    sa.event.listen(e, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    db.create_all(e)
    exp = write_export(tmp_path / "x", households=(HOUSEHOLD, DUP), prospects=(PROSPECT,),
                       entities=(ENTITY, ENT2), tasks=(TASK, TASK2))
    res = loader.load_export(e, exp, progress=lambda m: None)
    assert res.status == "loaded", res.problems
    return e, exp


def test_candidates_and_compare(loaded):
    e, _ = loaded
    pairs = merge.find_candidates(e, "family_group")
    assert pairs[0]["a"] == "H-101" and pairs[0]["b"] == "H-102"
    assert "same name" in pairs[0]["reasons"]
    assert any(p["a"] == "E-301" and "same ABN" in p["reasons"]
               for p in merge.find_candidates(e, "entity"))
    cmp = merge.compare(e, "family_group", "H-101", "H-102")
    states = {f["key"]: f["state"] for f in cmp["fields"]}
    assert states["adviser"] == "conflict" and states["city"] == "a_only"
    assert states["ins_renewal"] == "b_only" and states["ff:marital"] == "same"
    assert states["ffgroup:fund"] == "b_only"
    assert cmp["children"]["b"]["file notes"] == 1


def test_merge_rules_and_moves(loaded):
    e, exp = loaded
    rep = merge.apply_merge(e, "family_group", "H-101", "H-102",
                            picks={"email": "H-102"}, exclude={"notes"}, actor="Tester")
    with e.connect() as c:
        fg = c.execute(sa.select(db.family_group).where(db.family_group.c.id == "H-101")).mappings().one()
        assert fg["adviser"] == "Pat Adviser"          # conflict: kept record wins by default
        assert fg["email"] == "sam@example.com"        # conflict: picked the dropped record
        assert str(fg["ins_renewal"]) == "2027-01-31"  # blank filled from the dropped record
        assert fg["city"] == "Testville"               # filled value kept
        assert fg["notes"] is None                     # excluded: stays blank
        assert fg["ff_answers"]["fund#0.superName"] == "Other Super"
        assert c.execute(sa.select(sa.func.count()).select_from(db.family_group).where(
            db.family_group.c.id == "H-102")).scalar_one() == 0
        notes = c.execute(sa.select(sa.func.count()).select_from(db.file_note).where(
            db.file_note.c.family_group_id == "H-101")).scalar_one()
        assert notes == 2
        emails = sorted(r[0] for r in c.execute(sa.select(db.contact_point.c.value).where(
            db.contact_point.c.family_group_id == "H-101", db.contact_point.c.kind == "email")))
        assert emails == ["alex@example.com", "sam@example.com"]   # duplicate dropped
        accts = sorted(r[0] for r in c.execute(sa.select(db.account.c.id).where(
            db.account.c.family_group_id == "H-101")))
        assert accts == ["H-101:A1", "H-101:A1-2", "H-101:A2"]
        assert c.execute(sa.select(db.entity.c.home_family_group_id).where(
            db.entity.c.id == "E-302")).scalar_one() == "H-101"
        assert c.execute(sa.select(db.task.c.family_group_id).where(
            db.task.c.id == "T-402")).scalar_one() == "H-101"
        mr = c.execute(sa.select(db.merge_record)).mappings().one()
        assert mr["dropped_xplan_id"] == "102" and mr["snapshot"]["name"] == "Sam & Alex Sample"
    assert rep["fields"]["notes"] == "excluded"

    # a re-import of the same Xplan data doesn't bring H-102 back
    res = loader.load_export(e, exp, progress=lambda m: None)
    assert res.status == "loaded", res.problems
    assert any("merged into H-101" in w for w in res.warnings)
    assert loader.table_counts(e)["family_group"] == 2   # H-101 and the prospect


def test_entity_merge_folds_roles(loaded):
    e, _ = loaded
    merge.apply_merge(e, "entity", "E-301", "E-302")
    with e.connect() as c:
        roles = c.execute(sa.select(db.entity_role).where(
            db.entity_role.c.entity_id == "E-301")).mappings().all()
        ent = c.execute(sa.select(db.entity).where(db.entity.c.id == "E-301")).mappings().one()
    assert ent["details"]["accountant"] == "Acme" and ent["details"]["auditor"] == "Sample Audit"
    by_person = {(r["family_group_id"], r["person_pos"]): r for r in roles}
    assert set(by_person) == {("H-101", "1"), ("H-101", "2"), ("H-102", "1")}


def test_bad_pick_refused(loaded):
    e, _ = loaded
    with pytest.raises(merge.MergeError):
        merge.apply_merge(e, "family_group", "H-101", "H-102", picks={"name": "H-999"})
