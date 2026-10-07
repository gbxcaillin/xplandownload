"""Put the raw Xplan extract and the derived exports into Azure Blob storage.

Each set (raw, derived) is:
  1. hashed - MANIFEST-<set>.json lists every file with its size and SHA-256;
  2. packed into one 7-Zip archive with AES-256 (file names hidden too). 7-Zip asks for
     the password itself, so it never passes through this tool, .env or the command line;
  3. tested - 7-Zip reopens the archive with the password typed again;
  4. uploaded with AzCopy, signed in with your Microsoft account (no storage keys);
  5. checked - the MD5 Azure stores must match the archive's MD5.

Every step is skipped when its result is already there, so a re-run picks up where it
stopped. Nothing in the manifests or index is client data: the raw set lists the Iress zip
name, the derived set lists table/export file names.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

Progress = Callable[[str], None]

SETS = {  # set -> (container, access tier)
    "raw": ("xplan-raw", "Cold"),
    "derived": ("xplan-derived", "Cool"),
}
INDEX = "ARCHIVE_INDEX.json"
GB = 1024 ** 3


class VaultError(Exception):
    pass


def snapshot_label(names: list[str]) -> str:
    """'extract_..._202610021612.zip' -> '2026-10-02_1612'; else today's date."""
    for n in names:
        m = re.search(r"_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})\.zip$", n, re.I)
        if m:
            y, mo, d, h, mi = m.groups()
            return f"{y}-{mo}-{d}_{h}{mi}"
    return dt.date.today().isoformat()


def hash_file(path: Path, progress: Progress | None = None) -> dict:
    sha, md5, done = hashlib.sha256(), hashlib.md5(), 0
    size = path.stat().st_size
    next_report = 5 * GB
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            sha.update(chunk)
            md5.update(chunk)
            done += len(chunk)
            if progress and done >= next_report:
                progress(f"    hashing {path.name}: {done / GB:,.0f} of {size / GB:,.0f} GB")
                next_report += 5 * GB
    return {"size": size, "sha256": sha.hexdigest(),
            "md5": base64.b64encode(md5.digest()).decode()}


def collect(sources: list[Path]) -> list[tuple[Path, Path]]:
    """(file, path relative to its source root) for every file under the sources."""
    out = []
    for src in sources:
        if src.is_file():
            out.append((src, Path(src.name)))
        elif src.is_dir():
            out += [(p, p.relative_to(src.parent)) for p in sorted(src.rglob("*")) if p.is_file()]
        else:
            raise VaultError(f"Not found: {src}")
    return out


def build_manifest(name: str, snapshot: str, files: list[tuple[Path, Path]],
                   progress: Progress) -> dict:
    entries = []
    for path, rel in files:
        h = hash_file(path, progress)
        entries.append({"path": rel.as_posix(), "size": h["size"], "sha256": h["sha256"]})
    return {"set": name, "snapshot": snapshot, "created": dt.datetime.now().isoformat(timespec="seconds"),
            "files": len(entries), "bytes": sum(e["size"] for e in entries), "entries": entries}


def find_tool(explicit: str | None, names: list[str], windows_paths: list[str]) -> str:
    for cand in ([explicit] if explicit else []) + names:
        found = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if found:
            return found
    for p in windows_paths:
        if Path(p).is_file():
            return p
    raise VaultError(f"Can't find {names[0]}. Install it or pass its full path.")


def seven_zip(explicit: str | None = None) -> str:
    try:
        return find_tool(explicit or os.environ.get("SEVEN_ZIP"), ["7z", "7z.exe"],
                         [r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"])
    except VaultError:
        raise VaultError("7-Zip not found. Install it from https://www.7-zip.org (64-bit), "
                         "or set SEVEN_ZIP to the full path of 7z.exe.") from None


def azcopy(explicit: str | None = None) -> str:
    try:
        return find_tool(explicit or os.environ.get("AZCOPY"), ["azcopy", "azcopy.exe"], [])
    except VaultError:
        raise VaultError("AzCopy not found. Download it from https://aka.ms/downloadazcopy-v10-windows, "
                         "unzip azcopy.exe, and set AZCOPY in .env to its full path.") from None


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def pack(tool: str, archive: Path, inputs: list[Path], progress: Progress) -> None:
    """7-Zip, store only (the data is already compressed), AES-256 incl. file names.
    '-p' with no value makes 7-Zip ask for the password (twice) itself. Absolute input paths
    are stored by their last name only (e.g. 'output/...', 'extract_....zip')."""
    tmp = archive.with_name(archive.name + ".partial")
    tmp.unlink(missing_ok=True)
    progress(f"  Packing into {archive.name} - 7-Zip will ask for the archive password twice.")
    progress("  Use the password from your password manager; without it the archive can't be opened.")
    r = run([tool, "a", "-t7z", "-mx=0", "-mhe=on", "-bsp1", "-p", str(tmp.resolve()),
             *[str(p.resolve()) for p in inputs]])
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise VaultError(f"7-Zip failed (exit {r.returncode}).")
    tmp.replace(archive)


def test_archive(tool: str, archive: Path, progress: Progress) -> None:
    progress(f"  Testing {archive.name} - type the archive password once more.")
    r = run([tool, "t", "-p", "-bsp1", str(archive)])
    if r.returncode != 0:
        raise VaultError(f"7-Zip test failed for {archive} (wrong password or damaged). "
                         "Delete it and run again.")


def blob_url(account: str, container: str, snapshot: str, name: str) -> str:
    return f"https://{account}.blob.core.windows.net/{container}/{snapshot}/{name}"


def ensure_login(tool: str, tenant: str | None, progress: Progress) -> None:
    r = run([tool, "login", "status"], capture_output=True, text=True)
    if r.returncode == 0 and "not logged in" not in (r.stdout + r.stderr).lower():
        return
    progress("  Sign in to Azure: AzCopy shows a code - open the link, enter the code, and sign "
             "in with your Prosperum Microsoft account.")
    cmd = [tool, "login"] + ([f"--tenant-id={tenant}"] if tenant else [])
    if run(cmd).returncode != 0:
        raise VaultError("AzCopy sign-in failed.")


def remote_md5(tool: str, url: str) -> str | None:
    """Content-MD5 of the blob ('' if it has none), or None if it isn't there."""
    r = run([tool, "list", url, "--properties", "ContentMD5"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    name = url.rsplit("/", 1)[1]
    for line in r.stdout.splitlines():
        if name in line and "Content Length" in line:
            m = re.search(r"ContentMD5:\s*([A-Za-z0-9+/=]+)", line)
            return m.group(1) if m else ""
    return None


def upload(tool: str, path: Path, url: str, tier: str, progress: Progress) -> None:
    progress(f"  Uploading {path.name} ({path.stat().st_size / GB:,.2f} GB) -> {url}")
    r = run([tool, "copy", str(path), url, "--put-md5", "--overwrite=false",
             f"--block-blob-tier={tier}"])
    if r.returncode != 0:
        raise VaultError("AzCopy upload failed. Run the same command again - finished files are "
                         "skipped - or see the AzCopy log named above.")


def archive_set(name: str, sources: list[Path], staging: Path, account: str, *,
                tenant: str | None = None, seven: str | None = None, az: str | None = None,
                upload_it: bool = True, progress: Progress = print) -> dict:
    container, tier = SETS[name]
    files = collect(sources)
    if not files:
        raise VaultError(f"No files found for the {name} set in: " + ", ".join(map(str, sources)))
    snapshot = snapshot_label([p.name for p, _ in files])
    work = staging / snapshot
    work.mkdir(parents=True, exist_ok=True)
    total = sum(p.stat().st_size for p, _ in files)
    progress(f"\n[{name}] {len(files):,} file(s), {total / GB:,.2f} GB, snapshot {snapshot}")

    free = shutil.disk_usage(work).free
    archive = work / f"xplan-{name}-{snapshot}.7z"
    if not archive.exists() and total + 2 * GB > free:
        raise VaultError(f"Not enough space in {work}: the archive needs about {total / GB:,.0f} GB, "
                         f"{free / GB:,.0f} GB free. Pass --staging on a drive with more space.")

    manifest_path = work / f"MANIFEST-{name}.json"
    if manifest_path.exists():
        progress(f"  Manifest already made: {manifest_path.name}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        progress("  Hashing the source files (SHA-256) ...")
        manifest = build_manifest(name, snapshot, files, progress)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    tool7 = seven_zip(seven)
    if archive.exists():
        progress(f"  Archive already made: {archive.name}")
    else:
        pack(tool7, archive, [manifest_path] + [s for s in sources], progress)
        test_archive(tool7, archive, progress)

    progress("  Hashing the archive ...")
    ah = hash_file(archive, progress)
    record = {"set": name, "snapshot": snapshot, "archive": archive.name, **ah,
              "source_files": manifest["files"], "source_bytes": manifest["bytes"],
              "container": container, "tier": tier, "uploaded": None, "verified": False}

    if upload_it:
        tool = azcopy(az)
        ensure_login(tool, tenant, progress)
        for path in (manifest_path, archive):
            url = blob_url(account, container, snapshot, path.name)
            if remote_md5(tool, url) is None:
                upload(tool, path, url, "Cool" if path is manifest_path else tier, progress)
            else:
                progress(f"  Already in Azure: {path.name}")
        url = blob_url(account, container, snapshot, archive.name)
        got = remote_md5(tool, url)
        if got != ah["md5"]:
            raise VaultError(f"Check failed for {url}: Azure has MD5 {got or 'none'}, the local "
                             f"archive is {ah['md5']}. Don't delete anything; tell Claude.")
        record.update(uploaded=url, verified=True)
        progress(f"  Verified: Azure copy matches (MD5 {ah['md5']}).")

    _update_index(staging, record)
    return record


def _update_index(staging: Path, record: dict) -> None:
    path = staging / INDEX
    index = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    index = [r for r in index if (r["set"], r["snapshot"]) != (record["set"], record["snapshot"])]
    index.append(record)
    path.write_text(json.dumps(index, indent=2), encoding="utf-8")
