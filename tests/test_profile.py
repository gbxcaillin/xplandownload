from xplan_extract.profile import PERSONAL, mask


def test_mask_hides_values():
    assert mask("Jane Citizen") == "Aaaa Aaaaaaa"
    assert mask("0412 345 678") == "9999 999 999"
    assert mask("jane@example.com") == "a@a.a"
    assert mask("2000-02-29") == "9999-99-99"
    assert "…" in mask("x" * 100)


def test_personal_columns_never_show_values():
    for col in ["first_name", "surname", "email_address", "mobile", "postcode", "dob", "tfn",
                "bank_account", "subject"]:
        assert PERSONAL.search(col), col
    for col in ["type", "status", "state_vlu", "gender"]:
        assert not PERSONAL.search(col), col


def test_rare_picklist_values_hidden():
    import inspect

    from xplan_extract import profile
    src = inspect.getsource(profile._profile_table)
    assert "MIN_REPEATS" in src and r"\d{4}" in src


def test_person_shaped_values():
    from xplan_extract.profile import PERSON_SHAPED, STAFF_COLUMN
    assert PERSON_SHAPED.match("Citizen, Jane")
    assert not PERSON_SHAPED.match("Balanced")
    assert STAFF_COLUMN.search("client_adviser") and STAFF_COLUMN.search("modified_by")
    assert not STAFF_COLUMN.search("delegate_opt_in")
