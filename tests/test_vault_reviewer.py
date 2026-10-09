"""The vault's AI reviewer: queueing, the privacy rules on its output, the PDF and the
SharePoint hand-off. The agent itself is replaced by a stand-in, so nothing leaves the test."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "stack" / "vault"))

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from brightly_vault import privacy  # noqa: E402
from brightly_vault.config import Settings  # noqa: E402
from brightly_vault.reviewer import (REVIEW_SCHEMA, ReviewerSettings, _inside,  # noqa: E402
                                     build_task, process)
from brightly_vault.store import Store  # noqa: E402
from brightly_vault.web import build_admin, build_public  # noqa: E402

KEY = b"r" * 32
STAFF = {"X-Forwarded-Email": "pat@example.com.au", "X-Vault": "1"}
VALID_TFN = "123 456 782"     # passes the ATO check digit (a published test number)


def test_privacy_rules():
    out = privacy.clean({"a": f"TFN {VALID_TFN} on file", "b": ["Member 1234567890123"],
                         "c": "Balance $412,000.50 on 2026-06-30", "d": "phone 0400 000 000"})
    assert out["a"] == "TFN [TFN removed] on file"
    assert out["b"] == ["Member ****0123"]
    assert out["c"] == "Balance $412,000.50 on 2026-06-30"
    c = privacy.clean_text
    assert c("member 9876543210987.") == "member ****0987."
    assert c("Member 9876 5432 1098, then") == "Member ****1098, then"
    assert c("on 2026-06-30 and 30 06 2026") == "on 2026-06-30 and 30 06 2026"
    assert c("call 0412 345 678 or 0398765432") == "call 0412 345 678 or 0398765432"
    assert c("$1,234,567.89 and 412350") == "$1,234,567.89 and 412350"
    assert privacy.without_health({"ff": {"smoker12m": "No", "healthNotes": "x", "dob": "1960"},
                                   "people": [{"medical": 1, "name": "A"}]}) == \
        {"ff": {"dob": "1960"}, "people": [{"name": "A"}]}


def test_job_folder_lock(tmp_path):
    assert _inside(str(tmp_path / "a.pdf"), tmp_path)
    assert not _inside("/etc/passwd", tmp_path)
    assert not _inside(str(tmp_path / ".." / "x"), tmp_path)


def test_schema_is_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                walk(v)
        if node.get("type") == "array":
            walk(node["items"])
    walk(REVIEW_SCHEMA)


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=tmp_path / "data", key=KEY, public_url="http://testserver",
                 require_scan=False)
    (tmp_path / "data").mkdir()
    store = Store(s.data_dir, KEY)
    public = TestClient(build_public(s, store))
    admin = TestClient(build_admin(s, store))
    r = admin.post("/vault/api/links", headers=STAFF,
                   json={"client_name": "Jane Sample", "client_ref": "H-101"}).json()
    path = r["url"].replace("http://testserver", "")
    public.post(path + "/code", headers={"X-Vault": "1"}, json={"code": r["code"]})
    rs = ReviewerSettings(work_root=tmp_path / "work")
    return s, store, public, admin, path, r["link"]["id"], rs


def upload(public, path, name, data):
    return public.post(path + "/upload", content=data,
                       headers={"X-Vault": "1", "X-File-Name": name})


class FakeSharePoint:
    class cfg:
        clients_path = "General/XPlan Files/Clients"

    def __init__(self):
        self.uploads = []

    def client_folder(self, xplan_id):
        return f"General/XPlan Files/Clients/Active/Jane Sample ({xplan_id})"

    def upload(self, folder, name, data):
        self.uploads.append((folder, name, data))
        return "https://sharepoint.example/review.pdf"


def test_finished_queues_a_review_and_reviewer_writes_the_pdf(env):
    s, store, public, admin, path, link_id, rs = env
    upload(public, path, "Super statement.pdf", b"%PDF-1.4 balance 412000")
    upload(public, path, "Super statement.pdf", b"%PDF-1.4 second copy")
    assert public.post(path + "/done", headers={"X-Vault": "1"}).json()["ok"]
    review = store.claim_review()
    assert review and review["status"] == "running" and len(json.loads(review["file_ids"])) == 2
    seen = {}

    async def fake_agent(workdir, task, brand, settings):
        seen["files"] = sorted(p.name for p in workdir.iterdir())
        seen["content"] = (workdir / "Super statement.pdf").read_bytes()
        seen["task"] = task
        return {"summary": f"New super balance. TFN {VALID_TFN} seen.",
                "documents": [{"file": "Super statement.pdf", "type": "Super statement",
                               "date": "2026-06-30", "about_whom": "Jane",
                               "summary": "Annual statement for member 9876543210987"}],
                "suggested_updates": [{"area": "super_accounts", "field": "Balance",
                                       "current_value": "$380000", "suggested_value": "$412000",
                                       "source_file": "Super statement.pdf", "source_page": 1,
                                       "confidence": "high", "reason": "Newer statement"}],
                "flags": [{"severity": "info", "text": "Health information present in x.pdf",
                           "source_file": "x.pdf"}],
                "unreadable": []}, 0.42

    sp = FakeSharePoint()
    asyncio.run(process(review, store, s, rs, sp, agent=fake_agent))
    assert seen["files"] == ["Super statement (2).pdf", "Super statement.pdf"]
    assert seen["content"] == b"%PDF-1.4 balance 412000"           # decrypted for the agent
    assert "Jane Sample" in seen["task"] and "Not available" in seen["task"]
    assert not list((rs.work_root).glob("review-*"))               # job folder wiped
    done = store.review(review["id"])
    assert done["status"] == "done" and done["sharepoint_url"].startswith("https://")
    assert "1 suggested update(s), 1 flag(s)" in done["summary"]
    folder, name, pdf = sp.uploads[0]
    assert folder.endswith("Jane Sample (101)") and name.endswith(".pdf") and pdf[:4] == b"%PDF"
    # staff can download the same PDF from the vault
    d = admin.get(f"/vault/reviews/{review['id']}", headers=STAFF)
    assert d.status_code == 200 and d.content == pdf
    text = pdf_text(pdf)
    assert "412000" in text and "Balance" in text
    assert "123 456 782" not in text and "9876543210987" not in text and "0987" in text
    links = admin.get("/vault/api/links", headers=STAFF).json()["links"]
    assert links[0]["reviews"][0]["status"] == "done"
    # nothing new: no second review
    assert admin.post(f"/vault/api/links/{link_id}/review", headers=STAFF).status_code == 409
    upload(public, path, "Payslip.pdf", b"%PDF-1.4 pay")
    assert admin.post(f"/vault/api/links/{link_id}/review", headers=STAFF).json()["ok"]


def pdf_text(pdf: bytes) -> str:
    import io
    pypdf = pytest.importorskip("pypdf")
    return " ".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(pdf)).pages)


def test_failed_agent_marks_the_review_failed(env):
    s, store, public, admin, path, link_id, rs = env
    upload(public, path, "a.pdf", b"%PDF")
    store.enqueue_review(link_id, "test")
    review = store.claim_review()

    async def broken(*a):
        raise RuntimeError("model unavailable")

    asyncio.run(process(review, store, s, rs, None, agent=broken))
    r = store.review(review["id"])
    assert r["status"] == "failed" and "model unavailable" in r["error"]
    assert not list(rs.work_root.glob("review-*"))


def test_stale_and_idle(env):
    s, store, public, admin, path, link_id, rs = env
    upload(public, path, "a.pdf", b"%PDF")
    assert store.idle_unreviewed(quiet_minutes=-1) == [link_id]
    store.enqueue_review(link_id, "auto")
    assert store.idle_unreviewed(quiet_minutes=-1) == []
    store.claim_review()
    assert store.requeue_stale() == 1 and store.claim_review()["attempts"] == 2


def test_task_has_no_health_and_record_shape():
    task = build_task("Jane", "H-1", [{"name": "a.pdf", "uploaded_at": "2026-10-09T00:00:00"}],
                      privacy.without_health({"family_group": {"ff_answers": {
                          "smoker12m": "Yes", "occupation": "Teacher"}}}))
    assert "Teacher" in task and "smoker" not in task
