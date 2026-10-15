"""Optional Gmail integration via Google's OAuth 2.0 installed-app flow (loopback + PKCE).

Disabled unless ``JOBPILOT_GMAIL_ENABLED=true`` **and** you have run
``jobpilot gmail auth`` with your own OAuth client (see docs/GMAIL_SETUP.md).

Scopes (minimal):
- ``gmail.compose``  — create drafts and send *drafts you approved*.
- ``gmail.metadata`` — read message headers/thread structure to detect replies.
- ``gmail.readonly`` — only if ``JOBPILOT_GMAIL_ALLOW_READ_BODIES=true``, replacing
  ``gmail.metadata``, to import interview invitations. (Gmail rejects body reads when
  the metadata scope is present, so the two are never requested together.)

Tokens are stored in ``<data>/secrets/gmail_token.json`` (mode 0600, git-ignored).
No message is ever sent without an approved draft and an explicit confirmation.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import mimetypes
import os
import secrets
import time
import urllib.parse
import webbrowser
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import Application, EmailDraft, utcnow
from jobpilot.services import audit, contacts as contact_svc, drafts as draft_svc, settings_store, storage

log = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_METADATA = "https://www.googleapis.com/auth/gmail.metadata"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
TIMEOUT = 30


class GmailError(RuntimeError):
    pass


def scopes(settings: Settings) -> list[str]:
    return [SCOPE_COMPOSE, SCOPE_READONLY if settings.gmail_allow_read_bodies else SCOPE_METADATA]


def is_ready(settings: Settings) -> bool:
    return bool(settings.gmail_enabled and settings.gmail_token_file.exists())


def status(settings: Settings) -> dict[str, Any]:
    tok = _load_token(settings) if settings.gmail_token_file.exists() else None
    return {"enabled": settings.gmail_enabled, "client_secrets": settings.gmail_secrets_file.exists(),
            "client_secrets_path": str(settings.gmail_secrets_file), "authorized": tok is not None,
            "granted_scopes": (tok or {}).get("scope", "").split(), "email": (tok or {}).get("email"),
            "requested_scopes": scopes(settings), "read_bodies": settings.gmail_allow_read_bodies}


def _client_secrets(settings: Settings) -> dict[str, str]:
    path = settings.gmail_secrets_file
    if not path.exists():
        raise GmailError(f"OAuth client file not found at {path}. See docs/GMAIL_SETUP.md")
    data = json.loads(path.read_text())
    cfg = data.get("installed") or data.get("web")
    if not cfg or not cfg.get("client_id"):
        raise GmailError("Client secrets JSON must contain an 'installed' (Desktop app) client")
    return cfg


def _write_secret(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def _load_token(settings: Settings) -> dict | None:
    try:
        return json.loads(settings.gmail_token_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def build_auth_url(settings: Settings, redirect_uri: str, state: str, verifier: str) -> str:
    cfg = _client_secrets(settings)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    params = {"client_id": cfg["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(scopes(settings)), "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
    return f"{AUTH_URI}?{urllib.parse.urlencode(params)}"


def exchange_code(settings: Settings, code: str, redirect_uri: str, verifier: str,
                  http: requests.Session | None = None) -> dict:
    cfg = _client_secrets(settings)
    http = http or requests.Session()
    r = http.post(TOKEN_URI, data={"code": code, "client_id": cfg["client_id"],
                                   "client_secret": cfg.get("client_secret", ""), "redirect_uri": redirect_uri,
                                   "grant_type": "authorization_code", "code_verifier": verifier}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise GmailError(f"Token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    _write_secret(settings.gmail_token_file, tok)
    return tok


def authorize(settings: Settings, timeout: int = 300, open_browser: bool = True) -> dict:
    """Interactive loopback authorisation (run from the CLI)."""
    if not settings.gmail_enabled:
        raise GmailError("Set JOBPILOT_GMAIL_ENABLED=true first")
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(64)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<p>JobPilot: authorisation received. You can close this tab.</p>")

        def log_message(self, *args):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url = build_auth_url(settings, redirect_uri, state, verifier)
    print("Open this URL to authorise JobPilot (it only listens on 127.0.0.1):\n\n" + url + "\n")
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout
    while "code" not in result and "error" not in result and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "error" in result:
        raise GmailError(f"Authorisation denied: {result['error']}")
    if result.get("state") != state:
        raise GmailError("State mismatch or timeout — authorisation aborted")
    tok = exchange_code(settings, result["code"], redirect_uri, verifier)
    client = GmailClient(settings)
    tok["email"] = client.profile_email()
    _write_secret(settings.gmail_token_file, {**_load_token(settings), "email": tok["email"]})
    return tok


def revoke(settings: Settings, http: requests.Session | None = None) -> None:
    tok = _load_token(settings)
    if tok:
        try:
            (http or requests.Session()).post(REVOKE_URI, params={"token": tok.get("refresh_token") or tok.get("access_token")},
                                              timeout=TIMEOUT)
        except requests.RequestException:
            pass
    settings.gmail_token_file.unlink(missing_ok=True)


class GmailClient:
    def __init__(self, settings: Settings, http: requests.Session | None = None):
        if not settings.gmail_enabled:
            raise GmailError("Gmail integration is disabled")
        self.settings = settings
        self.http = http or requests.Session()

    def _token(self, force_refresh: bool = False) -> str:
        tok = _load_token(self.settings)
        if not tok:
            raise GmailError("Not authorised — run `jobpilot gmail auth`")
        if force_refresh or tok.get("expires_at", 0) - 60 < time.time():
            if not tok.get("refresh_token"):
                raise GmailError("Token expired and no refresh token — run `jobpilot gmail auth` again")
            cfg = _client_secrets(self.settings)
            r = self.http.post(TOKEN_URI, data={"client_id": cfg["client_id"], "client_secret": cfg.get("client_secret", ""),
                                                "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"},
                               timeout=TIMEOUT)
            if r.status_code != 200:
                raise GmailError(f"Token refresh failed: HTTP {r.status_code}")
            new = r.json()
            tok.update({"access_token": new["access_token"], "expires_at": time.time() + int(new.get("expires_in", 3600))})
            _write_secret(self.settings.gmail_token_file, tok)
        return tok["access_token"]

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in (1, 2):
            r = self.http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {self._token(attempt == 2)}"},
                                  timeout=TIMEOUT, **kw)
            if r.status_code == 401 and attempt == 1:
                continue
            if r.status_code >= 400:
                raise GmailError(f"Gmail API {method} {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json() if r.content else {}
        raise GmailError("Unauthorised")  # pragma: no cover

    def profile_email(self) -> str:
        return self._req("GET", "/profile").get("emailAddress", "")

    def create_draft(self, raw: str, thread_id: str | None = None) -> dict:
        msg: dict[str, Any] = {"raw": raw}
        if thread_id:
            msg["threadId"] = thread_id
        return self._req("POST", "/drafts", json={"message": msg})

    def send_draft(self, draft_id: str) -> dict:
        return self._req("POST", "/drafts/send", json={"id": draft_id})

    def thread(self, thread_id: str) -> dict:
        return self._req("GET", f"/threads/{urllib.parse.quote(thread_id)}",
                         params={"format": "metadata", "metadataHeaders": ["From", "Date", "Subject"]})

    def list_messages(self, q: str, max_results: int = 25) -> list[dict]:
        if not self.settings.gmail_allow_read_bodies:
            raise GmailError("Reading messages requires JOBPILOT_GMAIL_ALLOW_READ_BODIES=true and re-authorisation")
        return self._req("GET", "/messages", params={"q": q, "maxResults": max_results}).get("messages", [])

    def message_text(self, msg_id: str) -> tuple[dict[str, str], str]:
        m = self._req("GET", f"/messages/{urllib.parse.quote(msg_id)}", params={"format": "full"})
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        return headers, _payload_text(m.get("payload", {}))[:20000]


def _decode_body(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace") if data else ""


def _payload_text(payload: dict) -> str:
    """Readable text of a Gmail message payload.

    ``multipart/alternative`` holds the same content in several formats: use the plain-text
    version if present, else the HTML one (converted). Other multiparts are concatenated.
    """
    from jobpilot.services.text_utils import html_to_text

    mime = (payload.get("mimeType") or "").lower()
    parts = payload.get("parts") or []
    if mime == "text/plain":
        return _decode_body(payload)
    if mime == "text/html":
        return html_to_text(_decode_body(payload))
    if mime == "multipart/alternative":
        for preferred in ("text/plain", "text/html", "multipart/related", "multipart/mixed"):
            for p in parts:
                if (p.get("mimeType") or "").lower() == preferred:
                    text = _payload_text(p)
                    if text.strip():
                        return text
        return ""
    if mime.startswith("multipart/"):
        return "\n".join(t for t in (_payload_text(p) for p in parts) if t.strip())
    return ""


# ---- Draft / send / sync -----------------------------------------------------------

def build_mime(settings: Settings, draft: EmailDraft, from_addr: str | None) -> str:
    msg = EmailMessage()
    msg["To"] = draft.to_address
    if from_addr:
        msg["From"] = from_addr
    msg["Subject"] = draft.subject
    msg.set_content(draft.body)
    if draft.resume_version and draft.resume_version.file_path:
        path = storage.resolve(settings, draft.resume_version.file_path)
        ctype = draft.resume_version.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=draft.resume_version.original_filename or path.name)
    return _b64url(msg.as_bytes())


def _guard(session: Session, draft: EmailDraft) -> None:
    if draft.status != "approved":
        raise GmailError("Only approved drafts can be sent to Gmail")
    blocked = contact_svc.is_blocked(session, draft.to_address, draft.contact.company_id if draft.contact else None)
    if blocked or (draft.contact and draft.contact.do_not_contact):
        raise GmailError(blocked or "Contact is do-not-contact")


def create_gmail_draft(session: Session, settings: Settings, draft: EmailDraft, client: GmailClient) -> dict:
    _guard(session, draft)
    tok = _load_token(settings) or {}
    res = client.create_draft(build_mime(settings, draft, tok.get("email")), draft.gmail_thread_id)
    draft.gmail_draft_id = res.get("id")
    draft.gmail_thread_id = (res.get("message") or {}).get("threadId") or draft.gmail_thread_id
    audit.record(session, "gmail.draft_created", "email_draft", draft.id)
    return res


def sent_today_via_gmail(session: Session) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.sent_via == "gmail",
                                                                  EmailDraft.sent_at >= start)) or 0


def send_via_gmail(session: Session, settings: Settings, draft: EmailDraft, client: GmailClient, confirm: str) -> dict:
    if confirm != "SEND":
        raise GmailError("Explicit confirmation required")
    _guard(session, draft)
    limit = settings_store.get(session, "outreach")["daily_send_limit"]
    if sent_today_via_gmail(session) >= limit:
        raise GmailError(f"Daily send limit reached ({limit}). JobPilot does not do bulk outreach.")
    if not draft.gmail_draft_id:
        create_gmail_draft(session, settings, draft, client)
    res = client.send_draft(draft.gmail_draft_id)
    draft.gmail_message_id = res.get("id")
    draft.gmail_thread_id = res.get("threadId") or draft.gmail_thread_id
    draft_svc.mark_sent(session, draft, via="gmail")
    audit.record(session, "gmail.sent", "email_draft", draft.id)
    return res


def sync_replies(session: Session, settings: Settings, client: GmailClient) -> dict:
    """Detect replies on threads of messages you sent; suppresses follow-ups on reply."""
    me = (_load_token(settings) or {}).get("email", "").lower()
    checked = replies = 0
    for d in session.scalars(select(EmailDraft).where(EmailDraft.status == "sent", EmailDraft.gmail_thread_id.is_not(None),
                                                      EmailDraft.reply_received_at.is_(None))):
        checked += 1
        t = client.thread(d.gmail_thread_id)
        for m in t.get("messages", []):
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            sender = parseaddr(headers.get("from", ""))[1].lower()
            ts = int(m.get("internalDate", 0)) / 1000
            sent_ts = d.sent_at.timestamp() if d.sent_at else 0
            if sender and sender != me and ts > sent_ts:
                draft_svc.record_reply(session, d, f"Reply from {sender} (detected via Gmail)")
                replies += 1
                break
    return {"checked": checked, "replies": replies, "message": f"{replies} repl(ies) found in {checked} thread(s)"}


def import_interview_emails(session: Session, settings: Settings, client: GmailClient, days: int = 14) -> dict:
    """Scan recent mail for interview invitations → suggestions in the review queue (never auto-created)."""
    from jobpilot.services import interviews as iv_svc
    from jobpilot.services.text_utils import normalize_company

    apps = [a for a in session.scalars(select(Application).where(Application.applied_at.is_not(None)))
            if a.status in ("applied", "assessment", "interview")]
    if not apps:
        return {"scanned": 0, "suggestions": 0, "message": "no submitted applications to match"}
    msgs = client.list_messages(f"newer_than:{days}d (interview OR assessment OR invitation OR schedule)")
    found = 0
    for m in msgs:
        headers, body = client.message_text(m["id"])
        text = f"{headers.get('Subject', '')}\n{headers.get('From', '')}\n{body}"
        tnorm = normalize_company(text)
        for app in apps:
            cname = normalize_company(app.job.company_name)
            if cname and f" {cname} " in f" {tnorm} ":
                if iv_svc.suggest_from_text(session, app, text, source_ref=f"gmail:{m['id']}"):
                    found += 1
                break
    return {"scanned": len(msgs), "suggestions": found, "message": f"{found} interview suggestion(s) from {len(msgs)} message(s)"}
