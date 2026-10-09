"""Save the review PDF into the client's SharePoint folder (Microsoft Graph, app-only).

The Entra app needs the Graph application permission Sites.Selected, granted write access to the
one SharePoint site that holds XPlan Files (so it can't touch anything else).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("vault.sharepoint")
GRAPH = "https://graph.microsoft.com/v1.0"


class SharePointError(Exception):
    pass


@dataclass
class SharePointConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    site: str                  # e.g. prosperum.sharepoint.com:/sites/ProsperumTeamFiles
    clients_path: str          # e.g. General/XPlan Files/Clients
    drive_name: str = "Documents"

    @classmethod
    def from_env(cls, env) -> "SharePointConfig | None":
        need = ["GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "SHAREPOINT_SITE"]
        if not all(env.get(k) for k in need):
            return None
        return cls(env["GRAPH_TENANT_ID"], env["GRAPH_CLIENT_ID"], env["GRAPH_CLIENT_SECRET"],
                   env["SHAREPOINT_SITE"],
                   env.get("SHAREPOINT_CLIENTS_PATH", "General/XPlan Files/Clients").strip("/"),
                   env.get("SHAREPOINT_DRIVE", "Documents"))


class SharePoint:
    def __init__(self, cfg: SharePointConfig):
        self.cfg = cfg
        self._token: tuple[str, float] | None = None
        self._drive: str | None = None
        self._folders: dict[str, str] = {}      # xplan id -> path relative to the drive root
        self._folders_at = 0.0

    # -- plumbing ------------------------------------------------------------
    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        body = urllib.parse.urlencode({
            "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials"}).encode()
        url = f"https://login.microsoftonline.com/{self.cfg.tenant_id}/oauth2/v2.0/token"
        data = self._send(urllib.request.Request(url, data=body, method="POST"), auth=False)
        self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return self._token[0]

    def _send(self, req: urllib.request.Request, auth: bool = True):
        if auth:
            req.add_header("Authorization", f"Bearer {self.token()}")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 503, 504) and attempt < 3:
                    time.sleep(int(exc.headers.get("Retry-After", "5")))
                    continue
                detail = exc.read()[:300].decode(errors="replace")
                raise SharePointError(f"Graph {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(3)
                    continue
                raise SharePointError(f"Graph unreachable: {exc}") from exc

    def get(self, path: str):
        return self._send(urllib.request.Request(GRAPH + path))

    def drive_id(self) -> str:
        if not self._drive:
            site = self.get(f"/sites/{self.cfg.site}")
            drives = self.get(f"/sites/{site['id']}/drives")["value"]
            match = [d for d in drives if d["name"] == self.cfg.drive_name] or drives[:1]
            if not match:
                raise SharePointError("no document library on that site")
            self._drive = match[0]["id"]
        return self._drive

    def _children(self, path: str) -> list[dict]:
        q = urllib.parse.quote(path)
        url = f"/drives/{self.drive_id()}/root:/{q}:/children?$select=name,folder&$top=999"
        items = []
        while url:
            page = self.get(url)
            items += page.get("value", [])
            nxt = page.get("@odata.nextLink")
            url = nxt[len(GRAPH):] if nxt else None
        return items

    # -- client folders ------------------------------------------------------
    def client_folder(self, xplan_id: str) -> str | None:
        """Clients/Active/<Name> (<id>) or Clients/Inactive/..., found by the "(id)" ending."""
        if time.time() - self._folders_at > 3600:
            found: dict[str, str] = {}
            for group in ("Active", "Inactive", ""):
                base = f"{self.cfg.clients_path}/{group}".rstrip("/")
                try:
                    kids = self._children(base)
                except SharePointError:
                    continue
                for item in kids:
                    m = re.search(r"\((\d+)\)\s*$", item["name"]) if "folder" in item else None
                    if m and m.group(1) not in found:
                        found[m.group(1)] = f"{base}/{item['name']}"
            self._folders, self._folders_at = found, time.time()
        return self._folders.get(xplan_id)

    def upload(self, folder: str, name: str, data: bytes) -> str:
        q = urllib.parse.quote(f"{folder}/{name}")
        req = urllib.request.Request(
            f"{GRAPH}/drives/{self.drive_id()}/root:/{q}:/content?"
            f"@microsoft.graph.conflictBehavior=rename", data=data, method="PUT",
            headers={"Content-Type": "application/pdf"})
        item = self._send(req)
        return item.get("webUrl", "")
