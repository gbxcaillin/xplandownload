from pathlib import Path

import pytest

from xplan_extract.brightly.export import UnsafeDestination, check_destination, TOOL_DIR
from xplan_extract.brightly.xplan_map import (Entity, Household, html_to_text, iso_at, owner_pk,
                                              person_name, staff_name, suggest)


def test_names():
    assert person_name("Jane", "Citizen") == "Jane Citizen"
    assert person_name(None, None, "Citizen, Jane") == "Jane Citizen"
    assert staff_name("Adviser, Alex") == "Alex Adviser"


def test_html_to_text():
    assert html_to_text("<p>Hello &amp; welcome</p><p>Second</p>") == "Hello & welcome\n\nSecond"
    assert html_to_text(b"plain") == "plain"
    assert html_to_text("<style>x{}</style>Text") == "Text"


def test_iso_at_melbourne_time():
    import datetime as dt
    assert iso_at(dt.datetime(2026, 1, 15, 9, 0)).endswith("+11:00")   # daylight saving
    assert iso_at(dt.datetime(2026, 7, 15, 9, 0)).endswith("+10:00")


def test_owner_pk():
    client, partner = Entity(1, {}), Entity(2, {})
    hh = Household("H-1", [client, partner], client)
    assert owner_pk("Client", client, hh) == "1"
    assert owner_pk("Partner", client, hh) == "2"
    assert owner_pk("Joint", client, hh) == "joint"
    assert owner_pk("Client", partner, hh) == "2"     # a row on the partner's own record
    assert owner_pk("Other", client, hh) is None


def test_destination_guard(tmp_path):
    secure = tmp_path / "secure"
    assert check_destination(secure / "x", secure, False) == (secure / "x").resolve()
    with pytest.raises(UnsafeDestination):
        check_destination(TOOL_DIR / "output", None, False)
    with pytest.raises(UnsafeDestination):
        check_destination(tmp_path / "elsewhere", secure, False)
    assert check_destination(tmp_path / "elsewhere", secure, True)


def test_suggest():
    assert "health" in suggest("ufield_entity#sz", "current_health")
    assert suggest("x", "zzz") == ""
