"""The client vault (deploy/stack/vault): links, codes, encrypted uploads, virus blocking and
the staff side. Fictional data only."""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "stack" / "vault"))

from starlette.testclient import TestClient  # noqa: E402

from brightly_vault import crypto  # noqa: E402
from brightly_vault.config import ConfigError, Settings, load  # noqa: E402
from brightly_vault.scan import ScanError  # noqa: E402
from brightly_vault.store import Store  # noqa: E402
from brightly_vault.web import build_admin, build_public, safe_name  # noqa: E402

KEY = b"k" * 32
STAFF = {"X-Forwarded-Email": "pat@example.com.au"}
POST = {"X-Vault": "1"}


class FakeScanner:
    """Flags any file containing EICAR, like ClamAV with the standard test string."""
    down = False

    def __init__(self):
        self.data = b""

    async def open(self):
        if FakeScanner.down:
            raise ScanError("down")

    async def feed(self, data):
        self.data += data

    async def finish(self):
        return (False, "Eicar-Test-Signature") if b"EICAR" in self.data else (True, "clean")

    def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    FakeScanner.down = False
    s = Settings(data_dir=tmp_path, key=KEY, public_url="http://testserver", max_mb=1,
                 max_files=3)
    store = Store(tmp_path, KEY)
    public = TestClient(build_public(s, store, scanner_factory=FakeScanner))
    admin = TestClient(build_admin(s, store))
    return s, store, public, admin


def make_link(admin, **extra):
    r = admin.post("/vault/api/links", headers={**STAFF, **POST},
                   json={"client_name": "Jane Sample", "client_ref": "H-101", "days": 14, **extra})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["url"].replace("http://testserver", ""), body["code"], body["link"]["id"]


def unlock(public, path, code):
    r = public.post(path + "/code", headers=POST, json={"code": code})
    assert r.status_code == 200, r.text


def upload(public, path, name, data):
    return public.post(path + "/upload", content=data,
                       headers={**POST, "X-File-Name": name,
                                "Content-Type": "application/octet-stream"})


def test_config_requires_a_key():
    with pytest.raises(ConfigError):
        load({"DOMAIN": "x.example"})
    key = base64.b64encode(KEY).decode()
    s = load({"VAULT_KEY": key, "DOMAIN": "crm.example.com.au", "CLAMD_HOST": "clamav"})
    assert s.public_url == "https://crm.example.com.au" and s.require_scan


def test_encryption_round_trip_and_tamper(tmp_path):
    key = crypto.new_key()
    data = bytes(range(256)) * 9000          # > 2 chunks
    w = crypto.EncryptingWriter(tmp_path / "f", key)
    for i in range(0, len(data), 70000):
        w.write(data[i:i + 70000])
    w.close()
    raw = (tmp_path / "f").read_bytes()
    assert data[:1000] not in raw
    assert b"".join(crypto.decrypt_chunks(tmp_path / "f", key)) == data
    (tmp_path / "f").write_bytes(raw[:-10])  # truncated
    with pytest.raises(crypto.DecryptError):
        b"".join(crypto.decrypt_chunks(tmp_path / "f", key))


def test_safe_name():
    assert safe_name("..%2F..%2Fetc%2Fpasswd") == "passwd"
    assert safe_name("C:\\Users\\me\\Statement.pdf") == "Statement.pdf"
    assert safe_name("a<b>.pdf") == "a_b_.pdf"


def test_client_flow(env):
    s, store, public, admin = env
    path, code, link_id = make_link(admin, message="Please send your super statement.")
    page = public.get(path)
    assert page.status_code == 200 and "Jane Sample" in page.text and "6-digit code" in page.text
    assert "Content-Security-Policy" in page.headers
    # no upload before the code
    assert upload(public, path, "a.pdf", b"%PDF-1").status_code == 401
    assert public.post(path + "/code", headers=POST, json={"code": "000000" if code != "000000"
                                                            else "111111"}).status_code == 403
    unlock(public, path, code)
    assert "Drag and drop" in public.get(path).text
    r = upload(public, path, "Super%20statement.pdf", b"%PDF-1.7 statement")
    assert r.status_code == 200 and r.json()["name"] == "Super statement.pdf"
    # stored encrypted, readable by staff
    f = store.list_links()[0]["files"][0]
    assert b"statement" not in store.file_path(link_id, f["id"]).read_bytes()
    d = admin.get(f"/vault/files/{f['id']}", headers=STAFF)
    assert d.status_code == 200 and d.content == b"%PDF-1.7 statement"
    assert "attachment" in d.headers["content-disposition"]
    assert public.post(path + "/done", headers=POST).json()["ok"]


def test_blocks_viruses_types_size_and_count(env):
    s, store, public, admin = env
    path, code, link_id = make_link(admin)
    unlock(public, path, code)
    r = upload(public, path, "nasty.pdf", b"X5O!P%@AP EICAR test")
    assert r.status_code == 422 and "virus" in r.json()["error"]
    assert not list((store.files_dir / link_id).glob("*"))       # nothing kept
    assert upload(public, path, "run.exe", b"MZ").status_code == 415
    assert upload(public, path, "big.pdf", b"0" * (1024 * 1024 + 1)).status_code == 413
    assert upload(public, path, "empty.pdf", b"").status_code == 400
    for i in range(3):
        assert upload(public, path, f"p{i}.jpg", b"jpeg").status_code == 200
    assert upload(public, path, "p4.jpg", b"jpeg").status_code == 409
    FakeScanner.down = True
    path2, code2, _ = make_link(admin)
    unlock(public, path2, code2)
    assert upload(public, path2, "a.pdf", b"%PDF").status_code == 503


def test_wrong_codes_lock_the_link(env):
    s, store, public, admin = env
    path, code, link_id = make_link(admin)
    wrong = "123456" if code != "123456" else "654321"
    for _ in range(4):
        assert public.post(path + "/code", headers=POST, json={"code": wrong}).status_code == 403
    assert public.post(path + "/code", headers=POST, json={"code": wrong}).status_code == 423
    assert public.post(path + "/code", headers=POST, json={"code": code}).status_code == 410
    assert public.get(path).status_code == 410
    # staff reopen it
    assert admin.post(f"/vault/api/links/{link_id}/extend", headers={**STAFF, **POST},
                      json={"days": 7}).json()["ok"]
    unlock(public, path, code)


def test_closed_unknown_and_csrf(env):
    s, store, public, admin = env
    path, code, link_id = make_link(admin)
    assert public.get("/v/" + "x" * 43).status_code == 404
    assert public.post(path + "/code", json={"code": code}).status_code == 400   # no X-Vault
    admin.post(f"/vault/api/links/{link_id}/close", headers={**STAFF, **POST})
    assert public.get(path).status_code == 410


def test_staff_side_needs_sign_in(env):
    s, store, public, admin = env
    assert admin.get("/vault/").status_code == 403
    assert admin.get("/vault/api/links").status_code == 403
    assert admin.get("/vault/", headers=STAFF).status_code == 200
    assert admin.post("/vault/api/links", headers=STAFF,
                      json={"client_name": "x"}).status_code == 400      # no X-Vault
    path, code, link_id = make_link(admin)
    r = admin.get(f"/vault/api/links/{link_id}/reveal", headers=STAFF).json()
    assert r["code"] == code and r["url"].endswith(path)
    # the public app has no staff routes
    assert public.get("/vault/api/links", headers=STAFF).status_code == 404
    events = admin.get("/vault/api/audit", headers=STAFF).json()["events"]
    assert {"link created", "link and code shown"} <= {e["action"] for e in events}


def test_delete_and_audit_is_append_only(env):
    import sqlite3
    s, store, public, admin = env
    path, code, link_id = make_link(admin)
    unlock(public, path, code)
    upload(public, path, "a.pdf", b"%PDF")
    f = store.list_links()[0]["files"][0]
    assert admin.post(f"/vault/api/files/{f['id']}/delete", headers={**STAFF, **POST}).json()["ok"]
    assert not store.file_path(link_id, f["id"]).exists()
    assert admin.get(f"/vault/files/{f['id']}", headers=STAFF).status_code == 404
    with pytest.raises(sqlite3.DatabaseError):
        store.db.execute("DELETE FROM audit")
    assert store.snapshot().exists()


def test_clamd_protocol():
    """Scanner speaks clamd's INSTREAM protocol (checked against a stand-in clamd)."""
    import asyncio

    from brightly_vault.scan import Scanner

    async def fake_clamd(reader, writer):
        assert await reader.readuntil(b"\0") == b"zINSTREAM\0"
        data = b""
        while True:
            n = int.from_bytes(await reader.readexactly(4), "big")
            if n == 0:
                break
            data += await reader.readexactly(n)
        writer.write(b"stream: Eicar-Test-Signature FOUND\0" if b"EICAR" in data
                     else b"stream: OK\0")
        await writer.drain()
        writer.close()

    async def run():
        server = await asyncio.start_server(fake_clamd, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        results = []
        for payload in (b"x" * 600_000, b"has EICAR inside"):
            sc = Scanner("127.0.0.1", port)
            await sc.open()
            await sc.feed(payload)
            results.append(await sc.finish())
        server.close()
        with pytest.raises(ScanError):
            await Scanner("127.0.0.1", 1).open()
        return results

    assert asyncio.run(run()) == [(True, "clean"), (False, "Eicar-Test-Signature")]
