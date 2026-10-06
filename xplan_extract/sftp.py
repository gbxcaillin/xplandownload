"""Download the data extract from the Iress MFT (SFTP) account."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import posixpath
import stat
import time
from pathlib import Path
from typing import Callable

import paramiko

Progress = Callable[[str], None]
CHUNK = 1024 * 1024


class SftpError(Exception):
    pass


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _HostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use, but only when explicitly allowed."""

    def __init__(self, known_hosts: Path, accept_new: bool, progress: Progress):
        self.known_hosts = known_hosts
        self.accept_new = accept_new
        self.progress = progress

    def missing_host_key(self, client, hostname, key):
        fp = fingerprint(key)
        if not self.accept_new:
            raise SftpError(
                f"The server {hostname} is not yet trusted (host key {key.get_name()} {fp}).\n"
                "If this is your first connection, re-run with --accept-new-host-key to "
                f"trust it and save it to {self.known_hosts}."
            )
        client.get_host_keys().add(hostname, key.get_name(), key)
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self.known_hosts))
        self.progress(f"Trusted new host key for {hostname}: {key.get_name()} {fp}")


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def _fetch(sftp: paramiko.SFTPClient, remote: str, partial: Path, total: int, label: str,
           progress: Progress, report_every: float = 30.0) -> None:
    """Download ``remote`` into ``partial``, resuming from what is already there."""
    start = partial.stat().st_size if partial.exists() else 0
    if start > total:
        partial.unlink()
        start = 0
    if start:
        progress(f"  resuming from {_fmt_size(start)} already downloaded")
    done = start
    last = time.monotonic()
    last_done = start
    with sftp.open(remote, "rb") as src, open(partial, "ab") as dst:
        src.seek(start)
        src.prefetch(total - start, max_concurrent_requests=64)
        while done < total:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last >= report_every or done >= total:
                # Speed over the last interval, so a slowdown shows up straight away.
                rate = (done - last_done) / max(now - last, 1e-6)
                last, last_done = now, done
                eta = (total - done) / rate if rate else 0
                progress(f"  {label}: {done * 100 / total:5.1f}%  {_fmt_size(done)} of "
                         f"{_fmt_size(total)}  {_fmt_size(rate)}/s  "
                         f"about {_fmt_duration(eta)} left")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def download(
    host: str,
    port: int,
    username: str,
    password: str,
    dest_dir: Path,
    remote_dir: str = "/",
    pattern: str = "*",
    known_hosts: Path = Path("known_hosts"),
    accept_new_host_key: bool = False,
    overwrite: bool = False,
    progress: Progress = print,
) -> list[Path]:
    """Download all files in ``remote_dir`` matching ``pattern``. Returns local paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
    except OSError:
        pass
    if known_hosts.exists():
        client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(_HostKeyPolicy(known_hosts, accept_new_host_key, progress))

    progress(f"Connecting to {host}:{port} as {username} ...")
    try:
        client.connect(host, port=port, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=30,
                       banner_timeout=30, auth_timeout=30)
    except paramiko.AuthenticationException as exc:
        raise SftpError(
            f"Authentication failed ({exc}). Check the username/password from the encrypted "
            "email (copy & paste, no spaces before/after). If they are correct, the account "
            "may have expired (requests close after 2 weeks) or your IP may not be whitelisted."
        ) from exc
    except (OSError, paramiko.SSHException) as exc:
        if isinstance(exc, SftpError):
            raise
        raise SftpError(
            f"Could not connect to {host}:{port}: {exc}\n"
            "Common causes: your public IP address is not whitelisted by Iress, or a "
            "firewall/antivirus is blocking outbound port 22."
        ) from exc

    downloaded: list[Path] = []
    try:
        sftp = client.open_sftp()
        entries = [e for e in sftp.listdir_attr(remote_dir) if stat.S_ISREG(e.st_mode or 0)]
        progress(f"Remote files in {remote_dir}: " +
                 (", ".join(f"{e.filename} ({e.st_size:,} bytes)" for e in entries) or "none"))
        matches = [e for e in entries if fnmatch.fnmatch(e.filename, pattern)]
        if not matches:
            raise SftpError(f"No remote files match {pattern!r} in {remote_dir}")

        for entry in matches:
            remote = posixpath.join(remote_dir, entry.filename)
            local = dest_dir / entry.filename
            if local.exists() and local.stat().st_size == entry.st_size and not overwrite:
                progress(f"Already downloaded: {local}")
                downloaded.append(local)
                continue
            partial = local.with_name(local.name + ".partial")
            if overwrite:
                partial.unlink(missing_ok=True)
            progress(f"Downloading {remote} -> {local}")
            _fetch(sftp, remote, partial, entry.st_size or 0, entry.filename, progress)
            size = partial.stat().st_size
            if entry.st_size is not None and size != entry.st_size:
                raise SftpError(f"Size mismatch for {entry.filename}: got {size:,}, "
                                f"expected {entry.st_size:,}. Run the command again to resume.")
            partial.replace(local)
            downloaded.append(local)
    finally:
        client.close()
    return downloaded
