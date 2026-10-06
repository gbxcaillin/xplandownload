import pyzipper
import pytest

from xplan_extract.archive import ArchiveError, extract_zip, find_backups

PASSWORD = "Rt#e9&[7%)8"


def make_zip(path, files, password=PASSWORD):
    with pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as z:
        z.setpassword(password.encode())
        for name, data in files.items():
            z.writestr(name, data)


def test_extract_aes_zip(tmp_path):
    zp = tmp_path / "extract.zip"
    make_zip(zp, {"db/backup.BAK": b"data", "readme.txt": b"hi"})
    files = extract_zip(zp, tmp_path / "out", PASSWORD, progress=lambda m: None)
    assert sorted(p.name for p in files) == ["backup.BAK", "readme.txt"]
    assert [p.name for p in find_backups(files)] == ["backup.BAK"]
    assert (tmp_path / "out" / "db" / "backup.BAK").read_bytes() == b"data"


def test_wrong_password(tmp_path):
    zp = tmp_path / "extract.zip"
    make_zip(zp, {"a.bak": b"data"})
    with pytest.raises(ArchiveError, match="Wrong password"):
        extract_zip(zp, tmp_path / "out", "nope", progress=lambda m: None)
    assert not list((tmp_path / "out").glob("*"))


def test_missing_password(tmp_path):
    zp = tmp_path / "extract.zip"
    make_zip(zp, {"a.bak": b"data"})
    with pytest.raises(ArchiveError, match="password protected"):
        extract_zip(zp, tmp_path / "out", None, progress=lambda m: None)


def test_zip_slip_rejected(tmp_path):
    zp = tmp_path / "evil.zip"
    make_zip(zp, {"../escape.bak": b"x"})
    with pytest.raises(ArchiveError, match="unsafe"):
        extract_zip(zp, tmp_path / "out", PASSWORD, progress=lambda m: None)


def test_not_enough_space(tmp_path, monkeypatch):
    zp = tmp_path / "extract.zip"
    make_zip(zp, {"a.bak": b"data"})
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("U", (), {"free": 1024})())
    with pytest.raises(ArchiveError, match="Not enough disk space"):
        extract_zip(zp, tmp_path / "out", PASSWORD, progress=lambda m: None)
