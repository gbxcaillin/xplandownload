from types import SimpleNamespace

import pytest

from xplan_extract import sqlserver

GB = 1024 ** 3
FILES = [
    {"LogicalName": "data", "Type": "D", "Size": 60 * GB},
    {"LogicalName": "log", "Type": "L", "Size": 10 * GB},
]


def test_space_check_blocks_when_too_small(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlserver.shutil, "disk_usage", lambda p: SimpleNamespace(free=50 * GB))
    with pytest.raises(sqlserver.RestoreError, match="needs 70.0 GB"):
        sqlserver._check_space(FILES, str(tmp_path), str(tmp_path), lambda m: None, enforce=True)


def test_space_check_reports_only(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlserver.shutil, "disk_usage", lambda p: SimpleNamespace(free=50 * GB))
    messages = []
    sqlserver._check_space(FILES, str(tmp_path), str(tmp_path), messages.append, enforce=False)
    assert "Restored database needs 70.0 GB in total." in messages


def test_space_check_skips_remote_paths(monkeypatch):
    monkeypatch.setattr(sqlserver.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    sqlserver._check_space(FILES, "/var/opt/mssql/nowhere", "/var/opt/mssql/nowhere",
                           lambda m: None, enforce=True)


def test_server_path_for_share(tmp_path):
    share = tmp_path / "share"
    bak = tmp_path / "x.bak"
    bak.write_bytes(b"1")
    assert sqlserver.server_path_for(bak, share, "/var/opt/mssql/backups", lambda m: None) \
        == "/var/opt/mssql/backups/x.bak"
    assert (share / "x.bak").exists()
