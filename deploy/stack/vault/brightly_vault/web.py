"""The two web apps: public (client uploads) and staff (behind Microsoft sign-in)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import urllib.parse
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response, StreamingResponse)
from starlette.routing import Route

from . import crypto, mail, pages
from .config import Settings
from .scan import ScanError, Scanner
from .store import Store, iso, now, parse

log = logging.getLogger("vault")

ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff",
           ".webp", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".rtf", ".odt", ".ods",
           ".msg", ".eml", ".pages", ".numbers"}
NOTIFY_EVERY = dt.timedelta(minutes=10)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else
            (request.client.host if request.client else ""))


def safe_name(raw: str) -> str:
    name = urllib.parse.unquote(raw or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + "." + ext[:9]) if dot else name[:150]
    return name or "file"


def ext_of(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024


def html(body: str, nonce: str, status: int = 200) -> HTMLResponse:
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
           "connect-src 'self'; img-src 'self' data:; form-action 'self'; "
           "frame-ancestors 'none'; base-uri 'none'")
    return HTMLResponse(body, status, headers={
        "Content-Security-Policy": csp, "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"})


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status)


def same_origin_post(request: Request) -> bool:
    """POSTs must carry X-Vault: 1. Browsers only send a custom header cross-site after a CORS
    pre-flight, which this app never answers, so a foreign page can't post on someone's behalf."""
    return request.headers.get("x-vault") == "1"


# ----------------------------------------------------------------------------
# Public app: /v/<token>
# ----------------------------------------------------------------------------

def build_public(s: Settings, store: Store,
                 scanner_factory: Callable[[], Scanner] | None = None) -> Starlette:
    if scanner_factory is None and s.clamd_host:
        scanner_factory = lambda: Scanner(s.clamd_host, s.clamd_port)
    session_key = s.derived("session")
    secure_cookie = s.public_url.startswith("https://")

    def sign(link_id: str, expires: int) -> str:
        msg = f"{link_id}.{expires}"
        return msg + "." + hmac.new(session_key, msg.encode(), hashlib.sha256).hexdigest()

    def has_session(request: Request, link: dict) -> bool:
        value = request.cookies.get("vault_session", "")
        try:
            link_id, expires, sig = value.split(".")
        except ValueError:
            return False
        good = hmac.compare_digest(sign(link_id, int(expires)), value) if expires.isdigit() \
            else False
        return good and link_id == link["id"] and int(expires) > now().timestamp()

    def lookup(request: Request) -> tuple[dict | None, str]:
        token = request.path_params["token"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
            return None, "missing"
        link = store.link_by_token(token)
        if not link:
            return None, "missing"
        return link, store.state(link)

    async def page(request: Request) -> Response:
        nonce = secrets.token_urlsafe(16)
        link, state = lookup(request)
        if state != "open":
            if link:
                store.audit("client", f"link opened while {state}", link["id"],
                            ip=client_ip(request))
            return html(pages.closed_page(s, state, nonce), nonce, 404 if state == "missing" else 410)
        store.audit("client", "link opened", link["id"], ip=client_ip(request))
        return html(pages.public_page(s, link, has_session(request, link), nonce,
                                      sorted(ALLOWED)), nonce)

    async def code(request: Request) -> Response:
        if not same_origin_post(request):
            return error("Bad request", 400)
        link, state = lookup(request)
        if state != "open":
            return error("This link is no longer open. Please contact your adviser.", 410)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return error("Bad request", 400)
        if not store.check_code(link, str(body.get("code", ""))[:12], s.code_attempts):
            link = store.link(link["id"])
            store.audit("client", "wrong code", link["id"], ip=client_ip(request))
            if link["locked"]:
                store.audit("client", "link locked (too many wrong codes)", link["id"],
                            ip=client_ip(request))
                return error("Too many attempts. This link is now locked; please contact "
                             "your adviser.", 423)
            left = s.code_attempts - link["failed_attempts"]
            return error(f"That code isn't right. {left} attempt{'s' if left != 1 else ''} "
                         f"left.", 403)
        store.audit("client", "code accepted", link["id"], ip=client_ip(request))
        expires = int(min(now() + dt.timedelta(minutes=s.session_minutes),
                          parse(link["expires_at"])).timestamp())
        resp = JSONResponse({"ok": True})
        resp.set_cookie("vault_session", sign(link["id"], expires),
                        max_age=s.session_minutes * 60, path=f"/v/{request.path_params['token']}",
                        httponly=True, secure=secure_cookie, samesite="strict")
        return resp

    async def maybe_notify(link: dict, finished: bool = False) -> None:
        to = s.notify_to or link["created_by"]
        last = parse(link["last_notified_at"])
        if not mail.enabled(s) or "@" not in (to or ""):
            return
        if not finished and last and now() - last < NOTIFY_EVERY:
            return
        count = store.count_files(link["id"])
        what = "has finished uploading" if finished else "is uploading files"
        await mail.send(s, to, f"Vault: {link['client_name']} {what}",
                        f"{link['client_name']} {what} ({count} file(s) so far).\n\n"
                        f"Open the vault: {s.public_url}/vault/\n")
        store.mark(link["id"], "last_notified_at")

    async def upload(request: Request) -> Response:
        if not same_origin_post(request):
            return error("Bad request", 400)
        link, state = lookup(request)
        if state != "open":
            return error("This link is no longer open. Please contact your adviser.", 410)
        if not has_session(request, link):
            return error("Please enter your code again.", 401)
        if store.count_files(link["id"]) >= s.max_files:
            return error(f"This link has reached its limit of {s.max_files} files. "
                         f"Please contact your adviser.", 409)
        name = safe_name(request.headers.get("x-file-name", ""))
        if ext_of(name) not in ALLOWED:
            return error(f"{name}: this type of file can't be uploaded. Please send PDFs, "
                         f"photos or Office documents.", 415)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > s.max_bytes:
            return error(f"{name} is larger than {s.max_mb} MB.", 413)
        if scanner_factory is None and s.require_scan:
            return error("Uploads are paused for a moment (virus scanner not ready). "
                         "Please try again shortly.", 503)

        file_id = secrets.token_hex(10)
        folder = store.files_dir / link["id"]
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / f"{file_id}.part"
        key = crypto.new_key()
        writer = crypto.EncryptingWriter(part, key)
        digest = hashlib.sha256()
        scanner = scanner_factory() if scanner_factory else None
        if scanner:
            try:
                await scanner.open()
            except ScanError as exc:
                log.warning("scanner unavailable: %s", exc)
                store.audit("system", "scanner unavailable", link["id"], detail=str(exc))
                if s.require_scan:
                    writer.abort()
                    part.unlink(missing_ok=True)
                    return error("We couldn't check files just now. Please try again in a "
                                 "few minutes.", 503)
                scanner = None
        total = 0
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > s.max_bytes:
                    raise ValueError("too big")
                digest.update(chunk)
                writer.write(chunk)
                if scanner:
                    await scanner.feed(chunk)
            if total == 0:
                raise ValueError("empty")
            writer.close()
            clean, detail = (await scanner.finish()) if scanner else (True, "not scanned")
        except ScanError as exc:
            writer.abort()
            part.unlink(missing_ok=True)
            log.warning("scan failed for %s: %s", link["id"], exc)
            store.audit("system", "upload refused (scanner)", link["id"], detail=str(exc),
                        ip=client_ip(request))
            return error("We couldn't check this file just now. Please try again in a "
                         "few minutes.", 503)
        except ValueError as exc:
            writer.abort()
            part.unlink(missing_ok=True)
            if str(exc) == "empty":
                return error(f"{name} is empty.", 400)
            return error(f"{name} is larger than {s.max_mb} MB.", 413)
        except Exception:
            writer.abort()
            part.unlink(missing_ok=True)
            raise
        finally:
            if scanner:
                scanner.close()

        if not clean:
            part.unlink(missing_ok=True)
            store.audit("system", "upload blocked: virus found", link["id"],
                        detail=f"{name}: {detail}", ip=client_ip(request))
            return error(f"{name} was blocked by our virus scanner and has not been saved.", 422)
        part.replace(store.file_path(link["id"], file_id))
        store.add_file(file_id, link["id"], name, total, digest.hexdigest(), key,
                       "clean" if detail == "clean" else detail)
        store.audit("client", "file uploaded", link["id"], file_id,
                    detail=f"{name} ({human_size(total)})", ip=client_ip(request))
        await maybe_notify(store.link(link["id"]))
        return JSONResponse({"ok": True, "name": name, "size": total})

    async def done(request: Request) -> Response:
        if not same_origin_post(request):
            return error("Bad request", 400)
        link, state = lookup(request)
        if state != "open" or not has_session(request, link):
            return error("This link is no longer open.", 410)
        store.mark(link["id"], "finished_at")
        store.audit("client", "client finished", link["id"], ip=client_ip(request))
        store.enqueue_review(link["id"], "client finished")
        await maybe_notify(store.link(link["id"]), finished=True)
        return JSONResponse({"ok": True})

    async def health(request: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(routes=[
        Route("/healthz", health),
        Route("/v/{token}", page),
        Route("/v/{token}/code", code, methods=["POST"]),
        Route("/v/{token}/upload", upload, methods=["POST"]),
        Route("/v/{token}/done", done, methods=["POST"]),
    ])


# ----------------------------------------------------------------------------
# Staff app: /vault/ (only reachable through oauth2-proxy)
# ----------------------------------------------------------------------------

def build_admin(s: Settings, store: Store) -> Starlette:

    def staff(request: Request) -> str | None:
        email = (request.headers.get("x-forwarded-email") or "").strip().lower()
        if not email or (s.admin_emails and email not in s.admin_emails):
            return None
        return email

    def guard(handler):
        async def wrapped(request: Request) -> Response:
            who = staff(request)
            if not who:
                return PlainTextResponse("Sign-in required", 403)
            if request.method == "POST" and not same_origin_post(request):
                return error("Bad request", 400)
            request.state.who = who
            return await handler(request)
        return wrapped

    def review_json(r: dict) -> dict:
        return {"id": r["id"], "status": r["status"], "requested_by": r["requested_by"],
                "requested_at": r["requested_at"], "finished_at": r["finished_at"],
                "summary": r["summary"], "error": r["error"],
                "sharepoint_url": r["sharepoint_url"], "has_pdf": bool(r["pdf_key_enc"]),
                "files": len(json.loads(r["file_ids"]))}

    def link_json(l: dict) -> dict:
        return {
            "reviews": [review_json(r) for r in l.get("reviews", [])],
            "id": l["id"], "client_name": l["client_name"], "client_ref": l["client_ref"],
            "client_email": l["client_email"], "created_by": l["created_by"],
            "created_at": l["created_at"], "expires_at": l["expires_at"],
            "state": l["state"], "finished_at": l["finished_at"],
            "failed_attempts": l["failed_attempts"],
            "files": [{"id": f["id"], "name": f["name"], "size": f["size"],
                       "size_text": human_size(f["size"]), "uploaded_at": f["uploaded_at"],
                       "scan": f["scan"], "downloads": f["downloads"]} for f in l["files"]],
        }

    async def root(request: Request) -> Response:
        return RedirectResponse("/vault/", 307)

    @guard
    async def dashboard(request: Request) -> Response:
        nonce = secrets.token_urlsafe(16)
        return html(pages.admin_page(s, request.state.who, nonce, mail.enabled(s)), nonce)

    @guard
    async def links(request: Request) -> Response:
        if request.method == "GET":
            all_links = store.list_links()
            reviews = store.reviews_for([l["id"] for l in all_links])
            for l in all_links:
                l["reviews"] = reviews.get(l["id"], [])
            return JSONResponse({"links": [link_json(l) for l in all_links]})
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return error("Bad request", 400)
        name = str(body.get("client_name", "")).strip()[:120]
        if not name:
            return error("Enter the client's name.", 400)
        days = max(1, min(int(body.get("days") or s.default_days), s.max_days))
        email = str(body.get("client_email", "")).strip()[:200]
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return error("That email address doesn't look right.", 400)
        link, token, code = store.create_link(
            name, request.state.who, days, str(body.get("client_ref", "")).strip()[:60], email,
            str(body.get("message", "")).strip()[:1000])
        url = f"{s.public_url}/v/{token}"
        emailed = False
        if body.get("send_email") and email:
            emailed = await mail.send(
                s, email, f"{s.brand}: your secure upload link",
                f"Hello {name},\n\n"
                + (str(body.get("message")).strip() + "\n\n" if body.get("message") else "")
                + f"Please use this secure link to send us your documents:\n{url}\n\n"
                f"We'll give you a 6-digit code separately (by SMS or phone) to open it. "
                f"The link works until {parse(link['expires_at']).strftime('%d %B %Y')}.\n\n"
                f"Please don't reply to this email with attachments.\n\n{s.brand}\n")
            store.audit(request.state.who, "link emailed to client" if emailed
                        else "link email failed", link["id"])
        return JSONResponse({"ok": True, "link": link_json({**link, "files": [],
                                                            "state": store.state(link)}),
                             "url": url, "code": code, "emailed": emailed})

    @guard
    async def reveal(request: Request) -> Response:
        link = store.link(request.path_params["id"])
        if not link:
            return error("Not found", 404)
        token, code = store.secrets_of(link)
        store.audit(request.state.who, "link and code shown", link["id"])
        return JSONResponse({"ok": True, "url": f"{s.public_url}/v/{token}", "code": code})

    @guard
    async def close(request: Request) -> Response:
        link = store.link(request.path_params["id"])
        if not link:
            return error("Not found", 404)
        store.close_link(link["id"])
        store.audit(request.state.who, "link closed", link["id"])
        return JSONResponse({"ok": True})

    @guard
    async def extend(request: Request) -> Response:
        link = store.link(request.path_params["id"])
        if not link:
            return error("Not found", 404)
        try:
            days = int((await request.json()).get("days") or s.default_days)
        except (ValueError, json.JSONDecodeError, TypeError):
            days = s.default_days
        days = max(1, min(days, s.max_days))
        store.extend_link(link["id"], days)
        store.audit(request.state.who, f"link reopened for {days} days", link["id"])
        return JSONResponse({"ok": True})

    @guard
    async def download(request: Request) -> Response:
        f = store.file(request.path_params["id"])
        if not f or f["deleted_at"]:
            return PlainTextResponse("Not found", 404)
        path = store.file_path(f["link_id"], f["id"])
        if not path.exists():
            return PlainTextResponse("The file is missing on the server", 410)
        key = store.file_key(f)
        store.counted_download(f["id"])
        store.audit(request.state.who, "file downloaded", f["link_id"], f["id"], f["name"])
        quoted = urllib.parse.quote(f["name"])
        ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", f["name"])
        return StreamingResponse(crypto.decrypt_chunks(path, key),
                                 media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; "
                                   f"filename*=UTF-8''{quoted}",
            "Content-Length": str(f["size"]), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})

    @guard
    async def delete(request: Request) -> Response:
        f = store.file(request.path_params["id"])
        if not f or f["deleted_at"]:
            return error("Not found", 404)
        store.delete_file(f, request.state.who)
        store.audit(request.state.who, "file deleted", f["link_id"], f["id"], f["name"])
        return JSONResponse({"ok": True})

    @guard
    async def review_now(request: Request) -> Response:
        link = store.link(request.path_params["id"])
        if not link:
            return error("Not found", 404)
        review = store.enqueue_review(link["id"], request.state.who)
        if not review:
            return error("Nothing new to review (or a review is already waiting).", 409)
        return JSONResponse({"ok": True})

    @guard
    async def review_pdf(request: Request) -> Response:
        r = store.review(request.path_params["id"])
        path = store.review_pdf_path(r["id"]) if r else None
        if not r or not r["pdf_key_enc"] or not path.exists():
            return PlainTextResponse("Not found", 404)
        key = crypto.unseal(store.key, r["pdf_key_enc"], b"review:" + r["id"].encode())
        link = store.link(r["link_id"])
        store.audit(request.state.who, "review downloaded", r["link_id"], detail=r["id"])
        name = re.sub(r"[^A-Za-z0-9 ._-]", "_", f"Vault review - {link['client_name']}.pdf")
        return StreamingResponse(crypto.decrypt_chunks(path, key), media_type="application/pdf",
                                 headers={"Content-Disposition": f"attachment; filename=\"{name}\"",
                                          "Content-Length": str(r["pdf_size"]),
                                          "Cache-Control": "no-store"})

    @guard
    async def audit(request: Request) -> Response:
        return JSONResponse({"events": store.recent_audit(100)})

    return Starlette(routes=[
        Route("/vault", root),
        Route("/vault/", dashboard),
        Route("/vault/api/links", links, methods=["GET", "POST"]),
        Route("/vault/api/links/{id}/reveal", reveal),
        Route("/vault/api/links/{id}/close", close, methods=["POST"]),
        Route("/vault/api/links/{id}/extend", extend, methods=["POST"]),
        Route("/vault/files/{id}", download),
        Route("/vault/api/files/{id}/delete", delete, methods=["POST"]),
        Route("/vault/api/links/{id}/review", review_now, methods=["POST"]),
        Route("/vault/reviews/{id}", review_pdf),
        Route("/vault/api/audit", audit),
    ])
