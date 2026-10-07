import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from xplan_extract import vault

FAKE_7Z = """
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "t":
    sys.exit(0)
out = Path([a for a in args if not a.startswith("-") and a != "a"][0])
with out.open("wb") as f:
    for src in [a for a in args if not a.startswith("-") and a != "a"][1:]:
        p = Path(src)
        for q in ([p] if p.is_file() else sorted(p.rglob("*"))):
            if q.is_file():
                f.write(q.read_bytes())
"""

FAKE_AZCOPY = """
import base64, hashlib, shutil, sys
from pathlib import Path
store = Path(__file__).with_name("blobs")
args = sys.argv[1:]
if args[0] == "login":
    sys.exit(0)
def local(url):
    return store / url.split(".net/", 1)[1]
if args[0] == "copy":
    dest = local(args[2]); dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args[1], dest); sys.exit(0)
if args[0] == "list":
    # real AzCopy output format: lists the folder, 'name; ContentMD5: x; Content Length: y'
    d = local(args[1].rstrip("/"))
    print("INFO: Authenticating to source using Azure AD")
    for p in (sorted(d.iterdir()) if d.is_dir() else []):
        md5 = base64.b64encode(hashlib.md5(p.read_bytes()).digest()).decode()
        print(f"{p.name}; ContentMD5: {md5}; Content Length: {p.stat().st_size} B")
    sys.exit(0)
sys.exit(2)
"""


def script(path: Path, body: str) -> str:
    path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_snapshot_label():
    assert vault.snapshot_label(["extract_x_DM8093_202610021612.zip"]) == "2026-10-02_1612"


def test_archive_set_packs_uploads_verifies_and_resumes(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    seven = script(tools / "7z", FAKE_7Z)
    az = script(tools / "azcopy", FAKE_AZCOPY)
    src = tmp_path / "download"; src.mkdir()
    zip_ = src / "extract_test_DM1_202610021612.zip"
    zip_.write_bytes(b"x" * 1000)
    staging = tmp_path / "staging"
    lines = []

    rec = vault.archive_set("raw", [zip_], staging, "acct", seven=seven, az=az,
                            progress=lines.append)
    assert rec["verified"] and rec["snapshot"] == "2026-10-02_1612"
    assert rec["uploaded"] == ("https://acct.blob.core.windows.net/xplan-raw/2026-10-02_1612/"
                               "xplan-raw-2026-10-02_1612.7z")
    blob = tools / "blobs" / "xplan-raw" / "2026-10-02_1612"
    assert {p.name for p in blob.iterdir()} == {"MANIFEST-raw.json", "xplan-raw-2026-10-02_1612.7z"}
    manifest = json.loads((staging / "2026-10-02_1612" / "MANIFEST-raw.json").read_text())
    assert manifest["files"] == 1 and manifest["entries"][0]["path"] == zip_.name
    assert json.loads((staging / vault.INDEX).read_text())[0]["verified"] is True

    lines.clear()  # second run: everything already done
    vault.archive_set("raw", [zip_], staging, "acct", seven=seven, az=az, progress=lines.append)
    text = "\n".join(lines)
    assert "Archive already made" in text and "Already in Azure" in text
    assert len(json.loads((staging / vault.INDEX).read_text())) == 1


def test_mismatch_is_reported(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    seven = script(tools / "7z", FAKE_7Z)
    az = script(tools / "azcopy", FAKE_AZCOPY)
    d = tmp_path / "output"; d.mkdir(); (d / "t.json").write_text("{}")
    blob = tools / "blobs" / "xplan-derived" / vault.snapshot_label([])
    blob.mkdir(parents=True)
    (blob / f"xplan-derived-{vault.snapshot_label([])}.7z").write_bytes(b"different")
    with pytest.raises(vault.VaultError, match="Check failed"):
        vault.archive_set("derived", [d], tmp_path / "s", "acct", seven=seven, az=az,
                          progress=lambda m: None)


def test_missing_7zip_message(monkeypatch):
    monkeypatch.setattr(vault.shutil, "which", lambda c: None)
    monkeypatch.delenv("SEVEN_ZIP", raising=False)
    with pytest.raises(vault.VaultError, match="7-zip.org"):
        vault.seven_zip()


def test_failed_test_deletes_unfinished_archive(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    seven = script(tools / "7z", FAKE_7Z.replace('if args[0] == "t":\n    sys.exit(0)',
                                                 'if args[0] == "t":\n    sys.exit(2)'))
    d = tmp_path / "output"; d.mkdir(); (d / "t.json").write_text("{}")
    staging = tmp_path / "s"
    with pytest.raises(vault.VaultError, match="deleted"):
        vault.archive_set("derived", [d], staging, "acct", seven=seven, upload_it=False,
                          progress=lambda m: None)
    assert not [p for p in staging.rglob("*.7z*")]
