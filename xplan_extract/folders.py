"""Sort the saved client folders into Clients/Active and Clients/Inactive.

The ``documents`` export writes one folder per client::

    Clients/<Client name> (<entity id>)/...            (before the split)
    Clients/Active/<Client name> (<entity id>)/...     (after)
    Clients/Inactive/<Client name> (<entity id>)/...

Each folder's Xplan id comes from ``documents_index.csv`` (written by the export; it still
knows the id when a long name cut the "(id)" off the folder name), falling back to the
"(id)" at the end of the name. A folder goes to Active when any of its ids is active.

Nothing moves unless ``apply`` is set: a preview writes the same plan file, so it can be
checked first. Re-running is safe and moves folders back if a client's status changed
(e.g. after Scott's answers). Folders starting with "_" (no client / unlinked) stay put.
Moving inside a OneDrive/SharePoint synced folder is a rename: nothing is re-uploaded or
downloaded.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .documents import _long, move_folder

Progress = Callable[[str], None]
GROUPS = ("Active", "Inactive")
INDEX = "documents_index.csv"
ID_SUFFIX = re.compile(r"\((\d+)\)\s*$")


@dataclass
class FolderPlan:
    name: str
    ids: list[int]
    now_in: str           # "" (directly under Clients), "Active" or "Inactive"
    goes_to: str
    action: str           # "move", "stays" or "check"
    note: str = ""
    error: str = ""


@dataclass
class SplitResult:
    plans: list[FolderPlan] = field(default_factory=list)
    moved: int = 0
    index_rows_updated: int = 0
    plan_file: Path | None = None

    def count(self, action: str, goes_to: str | None = None) -> int:
        return sum(1 for p in self.plans if p.action == action
                   and (goes_to is None or p.goes_to == goes_to))

    @property
    def errors(self) -> list[FolderPlan]:
        return [p for p in self.plans if p.error]


def _parts(saved_as: str) -> list[str]:
    return [p for p in re.split(r"[\\/]", saved_as or "") if p]


def _client_folder(parts: list[str]) -> tuple[str, str] | None:
    """(group, folder name) of a saved_as path under Clients, or None."""
    if len(parts) < 3 or parts[0] != "Clients":
        return None
    if parts[1] in GROUPS:
        return (parts[1], parts[2]) if len(parts) >= 4 else None
    return "", parts[1]


def ids_from_index(index_path: Path) -> dict[str, set[int]]:
    """Folder name (lower case) -> the Xplan client ids whose files are in it."""
    out: dict[str, set[int]] = {}
    if not index_path.is_file():
        return out
    with open(_long(index_path), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            where = _client_folder(_parts(row.get("saved_as", "")))
            cid = (row.get("client_id") or "").strip()
            if where and cid.isdigit():
                out.setdefault(where[1].lower(), set()).add(int(cid))
    return out


def _folders(clients: Path) -> list[tuple[str, Path]]:
    found = []
    for group in ("", *GROUPS):
        base = clients / group if group else clients
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            if not group and child.name in GROUPS:
                continue
            found.append((group, child))
    return found


def plan_split(dest: Path, active_ids: set[int],
               index_ids: dict[str, set[int]] | None = None) -> list[FolderPlan]:
    clients = dest / "Clients"
    if not clients.is_dir():
        raise FileNotFoundError(f"No Clients folder in {dest}")
    if index_ids is None:
        index_ids = ids_from_index(dest / INDEX)
    plans = []
    for now_in, path in _folders(clients):
        ids = set(index_ids.get(path.name.lower(), ()))
        note = ""
        if not ids:
            m = ID_SUFFIX.search(path.name)
            if m:
                ids = {int(m.group(1))}
                note = "Xplan id taken from the folder name"
        if not ids:
            plans.append(FolderPlan(path.name, [], now_in, now_in, "check",
                                    "No Xplan id found for this folder: left where it is"))
            continue
        groups = {"Active" if i in active_ids else "Inactive" for i in ids}
        goes_to = "Active" if "Active" in groups else "Inactive"
        if len(groups) > 1:
            note = "Shared by an active and an inactive client: kept with Active"
        plans.append(FolderPlan(path.name, sorted(ids), now_in, goes_to,
                                "stays" if now_in == goes_to else "move", note))
    return plans


def _rewrite_index(index_path: Path, moved: dict[tuple[str, str], str]) -> int:
    """Point documents_index.csv at the folders' new places; a copy of the old one is kept."""
    if not moved or not index_path.is_file():
        return 0
    with open(_long(index_path), newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        head = next(reader, None)
        rows = list(reader)
    if not head or "saved_as" not in head:
        return 0
    si = head.index("saved_as")
    st = head.index("status") if "status" in head else None
    sep = "\\" if any("\\" in r[si] for r in rows[:50] if len(r) > si) else "/"
    changed = 0
    for r in rows:
        if len(r) <= si:
            continue
        parts = _parts(r[si])
        where = _client_folder(parts)
        if not where or (where[0], where[1].lower()) not in moved:
            continue
        group = moved[(where[0], where[1].lower())]
        rest = parts[3:] if where[0] else parts[2:]
        r[si] = sep.join(["Clients", group, where[1], *rest])
        if st is not None and len(r) > st:
            r[st] = group
        changed += 1
    if changed:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = index_path.with_name(f"{index_path.stem}.before-split-{stamp}.csv")
        os.replace(_long(index_path), _long(backup))
        tmp = index_path.with_suffix(".tmp")
        with open(_long(tmp), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(head)
            w.writerows(rows)
        os.replace(_long(tmp), _long(index_path))
    return changed


def write_plan(plans: list[FolderPlan], path: Path, applied: bool) -> None:
    with open(_long(path), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["folder", "xplan_ids", "now_in", "goes_to", "action", "note", "error"])
        for p in plans:
            action = p.action
            if applied and p.action == "move":
                action = "NOT moved" if p.error else "moved"
            w.writerow([p.name, "; ".join(map(str, p.ids)), p.now_in or "Clients",
                        p.goes_to or "Clients", action, p.note, p.error])


def split_folders(dest: Path, active_ids: set[int], apply: bool = False,
                  progress: Progress = print) -> SplitResult:
    clients = dest / "Clients"
    result = SplitResult(plan_split(dest, active_ids))
    moved: dict[tuple[str, str], str] = {}
    if apply:
        todo = [p for p in result.plans if p.action == "move"]
        for n, p in enumerate(todo, 1):
            old = clients / p.now_in / p.name if p.now_in else clients / p.name
            try:
                move_folder(old, clients / p.goes_to / p.name)
                if old.exists():
                    p.error = "Some files already existed in the new place; the rest stay here"
                moved[(p.now_in, p.name.lower())] = p.goes_to
                result.moved += 1
            except OSError as exc:
                p.error = f"{type(exc).__name__}: {exc.strerror or exc}"
            if n % 250 == 0 or n == len(todo):
                progress(f"  moved {n:,} of {len(todo):,} folders")
        result.index_rows_updated = _rewrite_index(dest / INDEX, moved)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    result.plan_file = dest / f"folder_split_{'done' if apply else 'preview'}_{stamp}.csv"
    write_plan(result.plans, result.plan_file, apply)
    return result
