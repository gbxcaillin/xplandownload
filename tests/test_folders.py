import csv

from xplan_extract.folders import ids_from_index, plan_split, split_folders

HEAD = ["kind", "client_id", "client_name", "other_client_ids", "date", "type", "subtype",
        "subject", "xplan_docid", "xplan_partid", "original_filename", "mimetype", "bytes",
        "saved_as", "status"]


def _row(cid, saved_as, status=""):
    return ["attachment", cid, "", "", "", "", "", "", "1", "1", "a.pdf", "", "1", saved_as,
            status]


def _setup(tmp_path):
    clients = tmp_path / "Clients"
    for rel in ["Jane Citizen (101)", "Bob Single (202)", "Active/Old Client (303)",
                "Inactive/Sam Sample (404)", "_No client", "Long Name Cut Off Here"]:
        (clients / rel).mkdir(parents=True)
        (clients / rel / "a.pdf").write_text("x")
    rows = [_row("101", "Clients\\Jane Citizen (101)\\a.pdf"),
            _row("202", "Clients\\Bob Single (202)\\a.pdf"),
            _row("303", "Clients\\Active\\Old Client (303)\\a.pdf", "Active"),
            _row("404", "Clients\\Inactive\\Sam Sample (404)\\a.pdf", "Inactive"),
            _row("505", "Clients\\Long Name Cut Off Here\\a.pdf"),
            _row("", "Clients\\_No client\\a.pdf")]
    with open(tmp_path / "documents_index.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEAD)
        w.writerows(rows)
    return clients


def test_ids_from_index(tmp_path):
    _setup(tmp_path)
    ids = ids_from_index(tmp_path / "documents_index.csv")
    assert ids["long name cut off here"] == {505}
    assert ids["old client (303)"] == {303}
    assert "_no client" not in ids


def test_plan_split(tmp_path):
    clients = _setup(tmp_path)
    (clients / "Unknown folder").mkdir()
    plans = {p.name: p for p in plan_split(tmp_path, active_ids={101, 404, 505})}
    assert (plans["Jane Citizen (101)"].action, plans["Jane Citizen (101)"].goes_to) == \
        ("move", "Active")
    assert plans["Bob Single (202)"].goes_to == "Inactive"
    assert plans["Old Client (303)"].goes_to == "Inactive"     # moved back out of Active
    assert plans["Sam Sample (404)"].goes_to == "Active"
    assert plans["Long Name Cut Off Here"].ids == [505]        # id from the index
    assert plans["Unknown folder"].action == "check"
    assert "_No client" not in plans


def test_preview_moves_nothing(tmp_path):
    clients = _setup(tmp_path)
    r = split_folders(tmp_path, {101}, apply=False, progress=lambda m: None)
    assert (clients / "Jane Citizen (101)").is_dir()
    assert r.plan_file.exists() and "preview" in r.plan_file.name
    assert r.count("move") == 4


def test_apply_moves_and_updates_index(tmp_path):
    clients = _setup(tmp_path)
    r = split_folders(tmp_path, {101, 404}, apply=True, progress=lambda m: None)
    assert r.moved == 5 and not r.errors
    assert (clients / "Active" / "Jane Citizen (101)" / "a.pdf").exists()
    assert (clients / "Inactive" / "Bob Single (202)" / "a.pdf").exists()
    assert (clients / "Inactive" / "Old Client (303)").is_dir()
    assert (clients / "Active" / "Sam Sample (404)").is_dir()
    assert (clients / "_No client").is_dir()
    with open(tmp_path / "documents_index.csv", encoding="utf-8-sig") as fh:
        rows = {r["client_id"]: r for r in csv.DictReader(fh)}
    assert rows["101"]["saved_as"] == "Clients\\Active\\Jane Citizen (101)\\a.pdf"
    assert rows["101"]["status"] == "Active"
    assert rows["303"]["saved_as"] == "Clients\\Inactive\\Old Client (303)\\a.pdf"
    assert rows[""]["saved_as"] == "Clients\\_No client\\a.pdf"
    assert list(tmp_path.glob("documents_index.before-split-*.csv"))
    # a second run has nothing left to do
    again = split_folders(tmp_path, {101, 404}, apply=True, progress=lambda m: None)
    assert again.count("move") == 0 and again.count("stays") == 5
