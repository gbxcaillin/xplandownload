from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    data_dir: Path
    key: bytes                      # 32-byte master key (VAULT_KEY), wraps each file's key
    public_url: str                 # https://crm.example.com.au
    brand: str = "Brightday"
    max_mb: int = 100               # per file
    max_files: int = 50             # per link
    default_days: int = 14
    max_days: int = 90
    code_attempts: int = 5
    session_minutes: int = 120
    clamd_host: str | None = None
    clamd_port: int = 3310
    require_scan: bool = True
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    notify_to: str | None = None    # fallback: the staff member who created the link
    admin_emails: set[str] = field(default_factory=set)   # empty: anyone who can sign in

    def derived(self, purpose: str) -> bytes:
        return hmac.new(self.key, purpose.encode(), hashlib.sha256).digest()

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load(env=os.environ) -> Settings:
    raw = env.get("VAULT_KEY", "")
    try:
        key = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_") if raw else b""
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ConfigError("VAULT_KEY must be 32 random bytes, base64 "
                          "(openssl rand -base64 32). Keep a copy in the password manager.")
    public_url = (env.get("VAULT_PUBLIC_URL") or
                  (f"https://{env['DOMAIN']}" if env.get("DOMAIN") else "")).rstrip("/")
    if not public_url:
        raise ConfigError("Set DOMAIN (or VAULT_PUBLIC_URL)")
    return Settings(
        data_dir=Path(env.get("VAULT_DATA_DIR", "/data")),
        key=key,
        public_url=public_url,
        brand=env.get("VAULT_BRAND", "Brightday"),
        max_mb=int(env.get("VAULT_MAX_MB", "100")),
        max_files=int(env.get("VAULT_MAX_FILES", "50")),
        default_days=int(env.get("VAULT_LINK_DAYS", "14")),
        clamd_host=env.get("CLAMD_HOST") or None,
        clamd_port=int(env.get("CLAMD_PORT", "3310")),
        require_scan=_bool(env.get("VAULT_REQUIRE_SCAN"), True),
        smtp_host=env.get("SMTP_HOST") or None,
        smtp_port=int(env.get("SMTP_PORT", "587")),
        smtp_user=env.get("SMTP_USER") or None,
        smtp_password=env.get("SMTP_PASSWORD") or None,
        smtp_from=env.get("SMTP_FROM") or env.get("SMTP_USER") or None,
        notify_to=env.get("VAULT_NOTIFY_TO") or None,
        admin_emails={e.strip().lower() for e in env.get("VAULT_ADMIN_EMAILS", "").split(",")
                      if e.strip()},
    )
