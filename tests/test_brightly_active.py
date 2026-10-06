import openpyxl

from xplan_extract.brightly.active import norm_name, read_active_list

HEAD = [None, None, "Transaction No", None, "Transaction  Date", "Policy Number", "Client No",
        "Client / Owner", "Product Name", "Type", "Transaction Type", None, "Adviser", None,
        "Amount", "GST", "Inc GST", "CRM Reference"]


def test_read_active_list(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None, None, None, None, None, None, "Report title"])
    ws.append(HEAD)
    ws.append([None, None, "Clients: Citizen, Jane"])
    ws.append([None, None, "123", None, "2026-01-01", "PF-001 23", "9001", "Citizen, Jane",
               "Wrap", "FEE", "Ongoing", None, "Alex Adviser", None, 1, 0, 1, "101"])
    ws.append([None, None, "Total"])
    ws.append([None, None, "Clients: Single, Bob"])
    ws.append([None, None, "124", None, "2026-01-01", "777", "9002", "Single, Bob",
               "Wrap", "FEE", "Ongoing", None, "Alex Adviser", None, 1, 0, 1, None])
    path = tmp_path / "list.xlsx"
    wb.save(path)
    listed = read_active_list(path)
    assert [c.name for c in listed] == ["Citizen, Jane", "Single, Bob"]
    assert listed[0].crm_refs == {101} and listed[0].policies == {"PF00123"}
    assert listed[1].crm_refs == set() and listed[1].policies == {"777"}


def test_norm_name():
    assert norm_name("Citizen, Jane") == norm_name("Mrs Jane Citizen") == "citizen jane"
