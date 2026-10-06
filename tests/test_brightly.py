import json
from pathlib import Path

from xplan_extract.brightly import (ExportWriter, FactFind, Schema, TFN_MARK, clean_date,
                                    clean_number, is_tfn, scrub_tfns)

SCHEMA = Path(__file__).parent / "fixtures" / "mini_factfind_schema.json"

# Fictional TFNs that pass the ATO check digit (none belongs to anyone).
VALID_TFN = "123 456 782"


def test_tfn_check_digit():
    assert is_tfn(VALID_TFN)
    assert not is_tfn("123 456 789")
    assert not is_tfn("0412 345 678")   # mobile number: 10 digits
    assert not is_tfn("111 111 111")


def test_scrub_tfns_everywhere():
    n = [0]
    rec = {"note": f"TFN is {VALID_TFN}, phone 0412 345 678", "list": [VALID_TFN.replace(" ", "")],
           "num": 123456782}
    out = scrub_tfns(rec, n)
    assert out["note"] == f"TFN is {TFN_MARK}, phone 0412 345 678"
    assert out["list"] == [TFN_MARK] and out["num"] is None
    assert n[0] == 3


def test_clean_values():
    assert clean_date("02/05/1964") == "1964-05-02"
    assert clean_date("2024-03-01T10:00:00") == "2024-03-01"
    assert clean_date("1800-01-01") is None
    assert clean_number("$1,250,000") == 1250000
    assert clean_number("12.5") == 12.5


def test_factfind_keys_and_validation():
    ff = FactFind(Schema(SCHEMA))
    assert ff.set("marital", "married", source="t.marital")
    assert ff.set("dob", "1964-05-02", person=1)
    assert ff.set("homeValue", "1,250,000", owner="Joint")
    assert ff.set("balance", 412000, group="fund", index=0)
    ff.set_repeat_person("fund", 0, 1)
    assert ff.set("riskProfile", "balanced", person=2)
    assert not ff.set("marital", "It's complicated", source="t.marital")
    assert not ff.set("dob", "1964-05-02")                 # person missing
    assert not ff.set("notAField", "x", source="t.col")
    d = ff.as_dict()
    assert d["a"]["marital"] == "Married" and d["a"]["dob@1"] == "1964-05-02"
    assert d["a"]["homeValue"] == 1250000 and d["a"]["homeValue~owner"] == "joint"
    assert d["a"]["fund#0.balance"] == 412000 and d["a"]["fund#0.person"] == "1"
    assert d["a"]["riskProfile@2"] == "Balanced" and d["n"] == {"fund": 1}
    assert {p.suggested_target for p in ff.problems} == {"marital", "dob", "notAField"}


def test_sensitive_fields_counted():
    ff = FactFind(Schema(SCHEMA))
    ff.set("smoker12m", "No", person=1)
    assert ff.sensitive_count == 1 and Schema(SCHEMA).is_sensitive("smoker12m")


def test_writer(tmp_path):
    w = ExportWriter(tmp_path, "test")
    w.write("households", {"id": "H1", "notes": f"tfn {VALID_TFN}"})
    manifest = w.close("# readme")
    line = json.loads((tmp_path / "households.jsonl").read_text())
    assert TFN_MARK in line["notes"]
    assert manifest["record_counts"]["households.jsonl"] == 1
    assert manifest["tfn_values_removed_from_text"] == 1
    assert (tmp_path / "unmapped.csv").read_text().startswith("xplan_table")
