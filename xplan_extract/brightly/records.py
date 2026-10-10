"""Locked copies of advice records for 7 years (step 7.2).

Each final advice document (SOA, ROA, signed consent, record of advice, file note PDF ...) is
copied to the Azure container `advice-records` with a *locked* time-based immutability policy:
nobody, including us and Microsoft support, can change or delete it before `locked_until`
(7 years from the record date, the Corporations Act s912G / ASIC RG 175 period). The SHA-256
fingerprint is kept in `advice_record` and in the blob's metadata, so any later copy can be
checked against the original.

Setup (once, in the Azure portal): create the container `advice-records` in the storage account
(version-level immutability is already on), then a SAS for it with Create, Write, Read, List and
Set Immutability Policy ("i") permissions, IP-restricted to the server -> ADVICE_RECORDS_SAS_URL.
No Delete permission.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import sqlalchemy as sa

from . import database as db

KEEP_YEARS = 7
API_VERSION = "2023-11-03"


class RecordError(Exception):
    pass


def locked_until(record_date: dt.date, years: int = KEEP_YEARS) -> dt.date:
    try:
        return record_date.replace(year=record_date.year + years)
    except ValueError:
        return dt.date(record_date.year + years, 2, 28)


def blob_name(family_group_id: str | None, kind: str, record_date: dt.date, sha256: str,
              file_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9 ._()-]", "_", Path(file_name).name)[:150]
    folder = re.sub(r"[^A-Za-z0-9-]", "_", family_group_id or "no-client")
    return f"{folder}/{record_date:%Y}/{record_date:%Y-%m-%d} {kind} {sha256[:10]} {safe}"


def _blob_url(sas_url: str, name: str) -> str:
    base, _, query = sas_url.partition("?")
    return f"{base.rstrip('/')}/{urllib.parse.quote(name)}?{query}"


def upload_locked(sas_url: str, name: str, data: bytes, until: dt.date, meta: dict[str, str],
                  opener=urllib.request.urlopen) -> None:
    """Put the blob once (never over an existing one) with a locked retention policy."""
    until_dt = dt.datetime.combine(until, dt.time(), dt.timezone.utc)
    headers = {
        "x-ms-version": API_VERSION,
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "application/octet-stream",
        "Content-MD5": base64.b64encode(hashlib.md5(data).digest()).decode(),
        "If-None-Match": "*",                                  # never overwrite
        "x-ms-immutability-policy-until-date": email.utils.format_datetime(until_dt,
                                                                           usegmt=True),
        "x-ms-immutability-policy-mode": "Locked",
    }
    for k, v in meta.items():
        headers[f"x-ms-meta-{k}"] = re.sub(r"[^\x20-\x7e]", "_", str(v))[:500]
    req = urllib.request.Request(_blob_url(sas_url, name), data=data, method="PUT",
                                 headers=headers)
    try:
        with opener(req, timeout=120) as r:
            if r.status not in (200, 201):
                raise RecordError(f"Azure answered {r.status}")
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise RecordError("That record is already locked in Azure") from exc
        detail = exc.read()[:300].decode(errors="replace")
        raise RecordError(f"Azure refused the upload ({exc.code}): {detail}") from exc


def lock(engine: sa.Engine, sas_url: str, path: Path, *, family_group_id: str | None,
         kind: str, record_date: dt.date, actor: str, title: str = "",
         opener=urllib.request.urlopen) -> dict:
    """Lock one file for 7 years and register it. Returns the advice_record row."""
    data = Path(path).read_bytes()
    if not data:
        raise RecordError("The file is empty")
    sha = hashlib.sha256(data).hexdigest()
    with engine.connect() as c:
        dup = c.execute(sa.select(db.advice_record.c.blob_path).where(
            db.advice_record.c.sha256 == sha)).first()
    if dup:
        raise RecordError(f"This exact file is already locked: {dup[0]}")
    until = locked_until(record_date)
    name = blob_name(family_group_id, kind, record_date, sha, Path(path).name)
    upload_locked(sas_url, name, data, until,
                  {"sha256": sha, "familygroup": family_group_id or "", "kind": kind,
                   "recorddate": record_date.isoformat(), "lockedby": actor}, opener=opener)
    row = {"family_group_id": family_group_id, "kind": kind, "title": title or Path(path).name,
           "record_date": record_date, "file_name": Path(path).name, "sha256": sha,
           "size": len(data), "blob_path": name, "locked_until": until, "created_by": actor,
           "created_at": dt.datetime.now(dt.timezone.utc)}
    with engine.begin() as c:
        c.execute(db.advice_record.insert().values(**row))
        if family_group_id:
            c.execute(db.change_log.insert().values(
                at=row["created_at"], actor=actor, record_type="family_group",
                record_id=family_group_id, kind="record_locked",
                message=f"{kind} locked until {until:%d %b %Y}: {row['title']}"))
    return row


def verify(path: Path, sha256: str) -> bool:
    """Is this copy the same as the locked original?"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha256
