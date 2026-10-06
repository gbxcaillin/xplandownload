from xplan_extract.documents import FolderNames, decode_text, extension_for, safe_name


def test_safe_name():
    assert safe_name('Re: a/b <c> "d"?', 90) == "Re_ a_b _c_ _d__"
    assert safe_name("con", 90) == "_con"
    assert safe_name("~$temp.docx", 90) == "temp.docx"
    assert safe_name("name. ", 90) == "name"
    assert safe_name("", 90, "File note") == "File note"
    long = "x" * 200 + ".pdf"
    assert safe_name(long, 90) == "x" * 86 + ".pdf"


def test_extension_for():
    assert extension_for("Statement.pdf", "application/pdf", b"%PDF") == ""
    assert extension_for("FDS", "application/msword", b"") == ".doc"
    assert extension_for("x", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         b"PK") == ".xlsx"
    assert extension_for("x", "application/octet-stream", b"%PDF-1.4") == ".pdf"
    assert extension_for("mail", "message/rfc822", b"") == ".eml"


def test_decode_text():
    assert decode_text("Hello — Jane".encode("utf-8")) == "Hello — Jane"
    assert decode_text("Hello — Jane".encode("utf-16-le")) == "Hello — Jane"
    assert decode_text("caf\xe9".encode("cp1252")) == "café"


def test_folder_names_unique_and_stable(tmp_path):
    names = FolderNames()
    assert names.take(tmp_path, "a.pdf", "7") == "a.pdf"
    assert names.take(tmp_path, "A.pdf", "8") == "A (8).pdf"
    assert names.take(tmp_path / "x", "a.pdf", "9") == "a.pdf"


def test_wait_for_space_pauses_until_free(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from xplan_extract import documents

    free = iter([1 * documents.GB, 2 * documents.GB, 20 * documents.GB])
    monkeypatch.setattr(documents.shutil, "disk_usage", lambda p: SimpleNamespace(free=next(free)))
    monkeypatch.setattr(documents.time, "sleep", lambda s: None)
    messages = []
    documents.wait_for_space(tmp_path, 100, 10 * documents.GB, messages.append)
    assert "Low disk space" in messages[0] and "continuing" in messages[-1]


def test_move_folder_merges(tmp_path):
    from xplan_extract.documents import _status_of, move_folder
    old = tmp_path / "Clients" / "Jane Citizen (101)"
    old.mkdir(parents=True)
    (old / "a.pdf").write_text("a")
    new = tmp_path / "Clients" / "Active" / "Jane Citizen (101)"
    move_folder(old, new)
    assert (new / "a.pdf").exists() and not old.exists()
    # merging into an existing target keeps both, never overwrites
    old.mkdir()
    (old / "a.pdf").write_text("different")
    (old / "b.pdf").write_text("b")
    move_folder(old, new)
    assert (new / "a.pdf").read_text() == "a" and (new / "b.pdf").exists()
    assert _status_of(new / "a.pdf", tmp_path) == "Active"
