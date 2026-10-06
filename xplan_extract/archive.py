"""Extract the password-protected zip that Iress provides (AES or ZipCrypto)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import pyzipper

Progress = Callable[[str], None]


class ArchiveError(Exception):
    pass


def extract_zip(
    zip_path: Path,
    dest_dir: Path,
    password: str | None,
    progress: Progress = print,
    overwrite: bool = False,
    _depth: int = 0,
) -> list[Path]:
    """Extract every file in ``zip_path`` into ``dest_dir``; nested zips are extracted too.

    Returns the extracted (non-zip) files.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()
    extracted: list[Path] = []
    pwd = password.encode("utf-8") if password else None

    try:
        zf = pyzipper.AESZipFile(zip_path)
    except pyzipper.BadZipFile as exc:
        raise ArchiveError(f"{zip_path} is not a valid zip file: {exc}") from exc

    with zf:
        if pwd:
            zf.setpassword(pwd)
        _check_free_space(zf, dest_root, overwrite)
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (dest_root / info.filename).resolve()
            if dest_root not in target.parents:
                raise ArchiveError(f"Refusing to extract unsafe path: {info.filename}")
            if info.flag_bits & 0x1 and not pwd:
                raise ArchiveError(
                    f"{zip_path.name} is password protected. Provide the zip password "
                    "(ZIP_PASSWORD in .env or --zip-password)."
                )
            if target.exists() and target.stat().st_size == info.file_size and not overwrite:
                progress(f"  already extracted: {target.name}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                progress(f"  extracting {info.filename} ({info.file_size:,} bytes)")
                tmp = target.with_name(target.name + ".partial")
                try:
                    with zf.open(info) as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                except RuntimeError as exc:
                    tmp.unlink(missing_ok=True)
                    if "password" in str(exc).lower():
                        raise ArchiveError(
                            f"Wrong password for {zip_path.name}. Copy it from the encrypted "
                            "email exactly (no leading/trailing spaces)."
                        ) from exc
                    raise
                tmp.replace(target)

            if target.suffix.lower() == ".zip" and _depth < 3:
                extracted += extract_zip(target, target.with_suffix(""), password, progress,
                                         overwrite, _depth + 1)
            else:
                extracted.append(target)
    return extracted


def _check_free_space(zf, dest_root: Path, overwrite: bool) -> None:
    needed = 0
    for info in zf.infolist():
        target = dest_root / info.filename
        if info.is_dir() or (target.exists() and target.stat().st_size == info.file_size
                             and not overwrite):
            continue
        needed += info.file_size
    free = shutil.disk_usage(dest_root).free
    margin = 2 * 1024 ** 3  # leave 2 GB for the system
    if needed + margin > free:
        gb = 1024 ** 3
        raise ArchiveError(
            f"Not enough disk space to extract: needs {needed / gb:,.1f} GB (+2 GB spare) but "
            f"only {free / gb:,.1f} GB is free in {dest_root}. Free up space, or extract to "
            "another drive with --extract-dir (e.g. --extract-dir D:\\SQLBackups)."
        )


def list_contents(zip_path: Path) -> list[tuple[str, int]]:
    """Names and uncompressed sizes (no password needed)."""
    with pyzipper.AESZipFile(zip_path) as zf:
        return [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]


def find_backups(files: list[Path]) -> list[Path]:
    return sorted(p for p in files if p.suffix.lower() == ".bak")
