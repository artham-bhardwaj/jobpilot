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
import re
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


class GmailAuthError(GmailError):
    """The stored authorisation can't be used (revoked, expired, missing): re-run ``jobpilot gmail auth``."""


class GmailScopeError(GmailError):
    """The token lacks a permission this action needs."""


class GmailAPIError(GmailError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class GmailNetworkError(GmailError):
    """No usable response. ``ambiguous``: the request may have reached Gmail and taken effect."""

    def __init__(self, message: str, ambiguous: bool):
        super().__init__(message)
        self.ambiguous = ambiguous


AUTH_EXPIRED_MSG = ("Gmail access was revoked or has expired (Google: invalid_grant). Run `jobpilot gmail auth` "
                    "again. If this happens every 7 days, your Google Cloud OAuth app is still in Testing mode: "
                    "publish it (docs/GMAIL_SETUP.md).")


def auth_error(settings: Settings) -> str | None:
    """Why the stored authorisation no longer works (Google rejected its refresh token), or None."""
    return (_load_token(settings) or {}).get("auth_error")


def scopes(settings: Settings) -> list[str]:
    return [SCOPE_COMPOSE, SCOPE_READONLY if settings.gmail_allow_read_bodies else SCOPE_METADATA]


def is_ready(settings: Settings) -> bool:
    return bool(settings.gmail_enabled and settings.gmail_token_file.exists())


def status(settings: Settings) -> dict[str, Any]:
    tok = _load_token(settings) if settings.gmail_token_file.exists() else None
    return {"enabled": settings.gmail_enabled, "client_secrets": settings.gmail_secrets_file.exists(),
            "client_secrets_path": str(settings.gmail_secrets_file),
            "authorized": tok is not None and not tok.get("auth_error"), "auth_error": (tok or {}).get("auth_error"),
            "granted_scopes": (tok or {}).get("scope", "").split(), "email": (tok or {}).get("email"),
            "requested_scopes": scopes(settings), "read_bodies": settings.gmail_allow_read_bodies,
            **{f"ready_{k}": v for k, v in readiness(settings).items()}}


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
            raise GmailAuthError("Not authorised — run `jobpilot gmail auth`")
        if force_refresh or tok.get("expires_at", 0) - 60 < time.time():
            if not tok.get("refresh_token"):
                raise GmailAuthError("Token expired and no refresh token — run `jobpilot gmail auth` again")
            cfg = _client_secrets(self.settings)
            r = self.http.post(TOKEN_URI, data={"client_id": cfg["client_id"], "client_secret": cfg.get("client_secret", ""),
                                                "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"},
                               timeout=TIMEOUT)
            if r.status_code != 200:
                if "invalid_grant" in r.text:
                    # Remember it, so status / readiness / health stop saying "authorized" until the next sign-in
                    # (exchange_code writes a fresh token without this flag).
                    _write_secret(self.settings.gmail_token_file,
                                  {**tok, "auth_error": AUTH_EXPIRED_MSG, "auth_error_at": time.time()})
                    raise GmailAuthError("Token refresh failed: " + AUTH_EXPIRED_MSG)
                raise GmailAuthError(f"Token refresh failed (HTTP {r.status_code}). Run `jobpilot gmail auth` again.")
            new = r.json()
            tok.update({"access_token": new["access_token"], "expires_at": time.time() + int(new.get("expires_in", 3600))})
            _write_secret(self.settings.gmail_token_file, tok)
        return tok["access_token"]

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in (1, 2):
            token = self._token(attempt == 2)
            try:
                r = self.http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
                                      timeout=TIMEOUT, **kw)
            except requests.ConnectTimeout as exc:
                raise GmailNetworkError(f"Could not reach Gmail ({type(exc).__name__})", ambiguous=False) from exc
            except requests.ConnectionError as exc:
                # Includes connections dropped after the request was sent: Gmail may have acted on it.
                raise GmailNetworkError(f"Connection to Gmail failed ({type(exc).__name__})", ambiguous=True) from exc
            except requests.RequestException as exc:  # read timeouts and the like: the request was sent
                raise GmailNetworkError(f"No response from Gmail ({type(exc).__name__})", ambiguous=True) from exc
            if r.status_code == 401 and attempt == 1:
                continue
            if r.status_code >= 400:
                raise _api_error(method, path, r)
            return r.json() if r.content else {}
        raise GmailAuthError("Gmail rejected the credentials. Run `jobpilot gmail auth` again.")  # pragma: no cover

    def profile_email(self) -> str:
        return self._req("GET", "/profile").get("emailAddress", "")

    def create_draft(self, raw: str, thread_id: str | None = None) -> dict:
        msg: dict[str, Any] = {"raw": raw}
        if thread_id:
            msg["threadId"] = thread_id
        return self._req("POST", "/drafts", json={"message": msg})

    def get_draft(self, draft_id: str, fmt: str = "metadata") -> dict:
        return self._req("GET", f"/drafts/{urllib.parse.quote(draft_id)}", params={"format": fmt})

    def list_drafts(self, max_results: int = 100) -> list[dict]:
        return self._req("GET", "/drafts", params={"maxResults": max_results}).get("drafts", [])

    def send_draft(self, draft_id: str) -> dict:
        return self._req("POST", "/drafts/send", json={"id": draft_id})

    def thread(self, thread_id: str) -> dict:
        return self._req("GET", f"/threads/{urllib.parse.quote(thread_id)}",
                         params={"format": "metadata", "metadataHeaders": ["From", "Date", "Subject"]})

    def list_messages(self, q: str, max_results: int = 25) -> list[dict]:
        if not self.settings.gmail_allow_read_bodies:
            raise GmailError("Reading messages requires JOBPILOT_GMAIL_ALLOW_READ_BODIES=true and re-authorisation")
        return self._req("GET", "/messages", params={"q": q, "maxResults": max_results}).get("messages", [])

    def recent_inbox(self, max_results: int = 50) -> list[dict]:
        """Newest inbox message ids. No search query: the metadata scope doesn't allow one, and none is needed."""
        return self._req("GET", "/messages", params={"labelIds": "INBOX", "maxResults": max_results}).get("messages", [])

    def message_headers(self, msg_id: str) -> dict[str, str]:
        """From / Subject / Date of one message (metadata scope; the body is never fetched)."""
        m = self._req("GET", f"/messages/{urllib.parse.quote(msg_id)}",
                      params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
        return {h["name"]: h["value"] for h in (m.get("payload") or {}).get("headers", [])}

    def message_text(self, msg_id: str) -> tuple[dict[str, str], str]:
        m = self._req("GET", f"/messages/{urllib.parse.quote(msg_id)}", params={"format": "full"})
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        return headers, _payload_text(m.get("payload", {}))[:20000]


def _api_error(method: str, path: str, r: requests.Response) -> GmailError:
    """A readable, actionable error from a Gmail API error response (never includes credentials)."""
    try:
        err = (r.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    detail = err.get("message") or r.text[:200]
    reasons = " ".join(str(e.get("reason", "")) for e in (err.get("errors") or []) if isinstance(e, dict))
    blob = f"{detail} {reasons} {err.get('status', '')}".lower()
    if r.status_code == 403 and ("insufficient" in blob or "scope" in blob):
        return GmailScopeError(f"Gmail refused {method} {path}: the granted permissions are insufficient ({detail}). "
                               "Run `jobpilot gmail auth` again and grant every requested permission.")
    if r.status_code == 401:
        return GmailAuthError("Gmail rejected the credentials (HTTP 401). Run `jobpilot gmail auth` again.")
    return GmailAPIError(f"Gmail API {method} {path}: HTTP {r.status_code}: {detail}", r.status_code)


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
#
# Local state (EmailDraft.status: draft → approved → sent) and Gmail state (EmailDraft.gmail_status) are separate.
# Approving changes only the local record. A Gmail draft exists only after Gmail answered a create request with its
# id; a message counts as sent via Gmail only after Gmail answered the send request (or a later check of the thread
# finds it in Sent). An outcome that can't be known is recorded as such and never retried blindly.

# Reserved top-level names (RFC 2606 / RFC 6761) used for placeholder addresses such as "recruiter@acme.example":
# never real mailboxes. A Gmail draft to them is fine (drafts aren't delivered); sending is refused.
_RESERVED_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local")
_ATTACH_CLAIM = re.compile(r"\b(resume|cv|résumé)\b[^.\n]{0,40}\battached\b|\battached\b[^.\n]{0,40}\b(resume|cv|résumé)\b",
                           re.I)


def reserved_recipient(address: str | None) -> str | None:
    """Why ``address`` must never be sent to (a reserved/example domain), or None."""
    domain = (parseaddr(address or "")[1].rpartition("@")[2] or "").lower().rstrip(".")
    if not domain:
        return "no recipient address"
    if any(domain.endswith(s) or domain == s[1:] for s in _RESERVED_SUFFIXES):
        return f"{domain} is a reserved placeholder domain, not a real mailbox"
    return None


def unverified_recipient(draft: EmailDraft) -> str | None:
    """Why the recipient isn't a verified address, or None. Sending needs a contact record with this exact address
    that you verified (with a note on how); a typed-in or guessed address can be drafted but never sent."""
    to = (draft.to_address or "").lower()
    c = draft.contact
    if c is None or (c.email or "").lower() != to:
        return (f"{draft.to_address or 'the recipient'} is not linked to a contact record — add it as a contact and "
                "verify it before sending")
    if c.email_status != "verified":
        return (f"{draft.to_address} is {c.email_status or 'unverified'} — verify it on the contact page "
                "(with a note on how you confirmed it) before sending")
    return None


def granted_scopes(settings: Settings) -> list[str] | None:
    """Scopes Google granted to the stored token, or None if the token doesn't say."""
    tok = _load_token(settings) or {}
    return tok.get("scope", "").split() or None


def _has_scope(granted: list[str], scope: str) -> bool:
    names = {g.rsplit("/", 1)[-1] for g in granted}
    short = scope.rsplit("/", 1)[-1]
    return short in names or (short == "gmail.metadata" and "gmail.readonly" in names) or \
        f"gmail.{short}" in names or short.removeprefix("gmail.") in names


def require_scope(settings: Settings, scope: str, purpose: str) -> None:
    granted = granted_scopes(settings)
    if granted is not None and not _has_scope(granted, scope):
        raise GmailScopeError(f"{purpose} needs the {scope.rsplit('/', 1)[-1]} permission, which this authorisation "
                              "does not include. Run `jobpilot gmail auth` again and grant it.")


def readiness(settings: Settings) -> dict[str, Any]:
    """Whether Gmail actions can run in *this* process, and if not, exactly why."""
    problems = []
    if not settings.gmail_enabled:
        problems.append("Gmail is not enabled for this JobPilot process: set JOBPILOT_GMAIL_ENABLED=true in .env "
                        "(or the environment of `jobpilot serve`) and restart the server")
    if not settings.gmail_secrets_file.exists():
        problems.append(f"OAuth client file missing at {settings.gmail_secrets_file}")
    if not settings.gmail_token_file.exists():
        problems.append("not authorised yet: run `jobpilot gmail auth`")
    elif auth_error(settings):
        problems.append(auth_error(settings))
    else:
        granted = granted_scopes(settings)
        if granted is not None and not _has_scope(granted, SCOPE_COMPOSE):
            problems.append("the authorisation lacks gmail.compose: run `jobpilot gmail auth` again")
    return {"ready": not problems, "problems": problems}


def _tailored_bytes(settings: Settings, draft: EmailDraft) -> tuple[bytes, str, str]:
    """The draft's validated tailored resume PDF, checked against its recorded checksum."""
    from sqlalchemy.orm import object_session

    from jobpilot.models import TailoredResume

    session = object_session(draft)
    tr = session.get(TailoredResume, draft.tailored_resume_id) if session is not None else None
    if tr is None or tr.status != "valid" or not tr.pdf_path:
        raise GmailError("The tailored resume for this draft is missing or no longer valid; not attaching it.")
    path = settings.files_dir / tr.pdf_path
    if not path.is_file():
        raise GmailError(f"The tailored resume file {tr.pdf_path} is missing; not attaching it.")
    data = path.read_bytes()
    if tr.pdf_sha256 and hashlib.sha256(data).hexdigest() != tr.pdf_sha256:
        raise GmailError("The tailored resume file does not match its recorded checksum; not attaching it.")
    return data, path.name, "application/pdf"


def _resume_bytes(settings: Settings, draft: EmailDraft) -> tuple[bytes, str, str] | None:
    """(bytes, filename, content type) of the draft's resume: its validated tailored resume if it has one, otherwise
    its own resume version, or None if it has neither."""
    if getattr(draft, "tailored_resume_id", None):
        return _tailored_bytes(settings, draft)
    v = draft.resume_version
    if v is None:
        return None
    if not v.file_path:
        raise GmailError(f"{v.label} has no stored file (it was generated without a PDF), so it cannot be attached. "
                         "Choose a resume version with a file, or remove the attachment sentence from the text.")
    try:
        path = storage.resolve(settings, v.file_path)
    except storage.StorageError as exc:
        raise GmailError(f"The file for {v.label} is missing from {settings.files_dir}; it cannot be attached. "
                         "Restore it from a backup or re-import the resume.") from exc
    data = path.read_bytes()
    if v.sha256 and hashlib.sha256(data).hexdigest() != v.sha256:
        raise GmailError(f"The stored file for {v.label} does not match its recorded checksum; not attaching it.")
    ctype = v.mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return data, v.original_filename or path.name, ctype


def build_message(settings: Settings, draft: EmailDraft, from_addr: str | None) -> tuple[str, dict | None, str]:
    """(base64url MIME, attachment info or None, content hash) for a draft. Raises if it can't be built honestly."""
    msg = EmailMessage()
    msg["To"] = draft.to_address
    if from_addr:
        msg["From"] = from_addr
    msg["Subject"] = draft.subject
    msg.set_content(draft.body)
    attachment = None
    resume = _resume_bytes(settings, draft)
    if resume is None and _ATTACH_CLAIM.search(draft.body or ""):
        raise GmailError("The text says a resume is attached, but this draft has no resume version. Remove that "
                         "sentence or recreate the draft with a resume.")
    if resume is not None:
        data, filename, ctype = resume
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        attachment = {"filename": filename, "content_type": ctype, "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "resume_version_id": draft.resume_version_id,
                      "tailored_resume_id": getattr(draft, "tailored_resume_id", None)}
    content = json.dumps([draft.to_address, draft.subject, draft.body, (attachment or {}).get("sha256")])
    return _b64url(msg.as_bytes()), attachment, hashlib.sha256(content.encode()).hexdigest()


def build_mime(settings: Settings, draft: EmailDraft, from_addr: str | None) -> str:
    return build_message(settings, draft, from_addr)[0]


def gmail_problems(session: Session, draft: EmailDraft) -> list[str]:
    """Why a draft can't be put into Gmail right now (local checks only)."""
    from jobpilot.services.profile import EMAIL_PATTERN

    problems = []
    if draft.status != "approved":
        problems.append("approve the draft in JobPilot first (Gmail gets only text you approved)")
    if not draft.to_address or not re.fullmatch(EMAIL_PATTERN, draft.to_address):
        problems.append("a valid recipient address is required")
    if not (draft.subject or "").strip() or not (draft.body or "").strip():
        problems.append("subject and body are required")
    if draft_svc.MISSING_RE.search(f"{draft.subject}\n{draft.body}"):
        problems.append("the text still contains [[MISSING: …]] markers")
    blocked = contact_svc.is_blocked(session, draft.to_address, draft.contact.company_id if draft.contact else None)
    if blocked:
        problems.append(blocked)
    if draft.contact and draft.contact.do_not_contact:
        problems.append("contact is do-not-contact")
    return problems


def _guard(session: Session, draft: EmailDraft) -> None:
    problems = gmail_problems(session, draft)
    if problems:
        raise GmailError("Cannot use Gmail for this draft: " + "; ".join(problems))


def _set_state(draft: EmailDraft, status: str | None, error: str | None = None) -> None:
    draft.gmail_status, draft.gmail_error, draft.gmail_synced_at = status, error, utcnow()


def _sent_message_in_thread(client: GmailClient, draft: EmailDraft, since_ts: float | None) -> dict | None:
    """A message in the draft's thread that Gmail labels SENT (and not DRAFT), sent at/after ``since_ts``."""
    if not draft.gmail_thread_id:
        return None
    for m in client.thread(draft.gmail_thread_id).get("messages", []):
        labels = set(m.get("labelIds") or [])
        if "SENT" in labels and "DRAFT" not in labels and \
                (since_ts is None or int(m.get("internalDate", 0)) / 1000 >= since_ts - 120):
            return m
    return None


def _ts(dt) -> float | None:
    from datetime import UTC

    return dt.replace(tzinfo=UTC).timestamp() if dt else None  # stored timestamps are naive UTC


def _find_orphan_draft(client: GmailClient, draft: EmailDraft) -> dict | None:
    """After a create request with an unknown outcome: the Gmail draft it may have made (same To and Subject)."""
    for item in client.list_drafts(100):
        got = client.get_draft(item["id"], "metadata")
        headers = {h["name"].lower(): h["value"] for h in (got.get("message") or {}).get("payload", {}).get("headers", [])}
        if parseaddr(headers.get("to", ""))[1].lower() == (draft.to_address or "").lower() and \
                headers.get("subject", "") == draft.subject:
            return got
    return None


def create_gmail_draft(session: Session, settings: Settings, draft: EmailDraft, client: GmailClient) -> dict:
    """Create the Gmail draft for an approved JobPilot draft. Returns {"id", "reused", "attachment"}.

    Never creates a second Gmail draft for the same content: an existing one is checked and reused, and after a
    request whose outcome was unknown, Gmail Drafts is searched before creating another.
    """
    _guard(session, draft)
    require_scope(settings, SCOPE_COMPOSE, "Creating a Gmail draft")
    tok = _load_token(settings) or {}
    raw, attachment, content_hash = build_message(settings, draft, tok.get("email"))

    if draft.gmail_draft_id and draft.gmail_content_hash == content_hash:
        try:
            got = client.get_draft(draft.gmail_draft_id, "minimal")
        except GmailAPIError as exc:
            if exc.status != 404:
                raise
            got = None
        if got:
            _set_state(draft, "created")
            return {"id": draft.gmail_draft_id, "reused": True, "attachment": draft.gmail_attachment}
        # Gone from Drafts: sent from Gmail, or deleted there. Find out before making another.
        sent = _sent_message_in_thread(client, draft, _ts(draft.approved_at))
        if sent:
            _record_gmail_sent(session, draft, sent)
            raise GmailError("This draft was already sent from Gmail; recorded it as sent. No new draft was created.")
        draft.gmail_draft_id = None
    if draft.gmail_status == "pending":
        orphan = _find_orphan_draft(client, draft)
        if orphan:
            draft.gmail_draft_id = orphan["id"]
            draft.gmail_thread_id = (orphan.get("message") or {}).get("threadId") or draft.gmail_thread_id
            draft.gmail_content_hash, draft.gmail_attachment = content_hash, attachment
            _set_state(draft, "created")
            audit.record(session, "gmail.draft_recovered", "email_draft", draft.id)
            return {"id": orphan["id"], "reused": True, "attachment": attachment}

    _set_state(draft, "pending")
    session.commit()  # the attempt is on record before Gmail is contacted
    try:
        res = client.create_draft(raw, draft.gmail_thread_id)
    except GmailNetworkError as exc:
        _set_state(draft, "pending" if exc.ambiguous else "failed",
                   f"{exc}. " + ("Gmail may have created the draft; retrying checks Gmail Drafts first."
                                 if exc.ambiguous else "No draft was created."))
        session.commit()
        raise GmailError(draft.gmail_error) from exc
    except GmailError as exc:
        _set_state(draft, "failed", str(exc))
        session.commit()
        raise
    if not res.get("id"):
        _set_state(draft, "failed", "Gmail answered without a draft id")
        session.commit()
        raise GmailError(draft.gmail_error)
    draft.gmail_draft_id = res["id"]
    draft.gmail_thread_id = (res.get("message") or {}).get("threadId") or draft.gmail_thread_id
    draft.gmail_content_hash, draft.gmail_attachment, draft.gmail_stale_draft_id = content_hash, attachment, None
    _set_state(draft, "created")
    audit.record(session, "gmail.draft_created", "email_draft", draft.id, attachment=bool(attachment))
    return {"id": res["id"], "reused": False, "attachment": attachment}


def sent_today_via_gmail(session: Session) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(select(func.count(EmailDraft.id)).where(EmailDraft.sent_via == "gmail",
                                                                  EmailDraft.sent_at >= start)) or 0


def _record_gmail_sent(session: Session, draft: EmailDraft, msg: dict) -> None:
    from datetime import UTC, datetime

    draft.gmail_message_id = msg.get("id") or draft.gmail_message_id
    draft.gmail_thread_id = msg.get("threadId") or draft.gmail_thread_id
    when = utcnow()
    if msg.get("internalDate"):
        when = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, UTC).replace(tzinfo=None)
    _set_state(draft, "sent")
    if draft.status == "approved":
        draft_svc.mark_sent(session, draft, via="gmail", when=when)
    audit.record(session, "gmail.sent", "email_draft", draft.id)


def send_preview(session: Session, settings: Settings, draft: EmailDraft) -> dict[str, Any]:
    """What a send would deliver, for the confirmation screen. Builds the message locally; contacts nothing."""
    problems = gmail_problems(session, draft)
    reserved = reserved_recipient(draft.to_address)
    if reserved:
        problems.append(f"sending is refused: {reserved}")
    elif unverified_recipient(draft):
        problems.append(f"sending is refused: {unverified_recipient(draft)}")
    if draft.gmail_status in ("send_unknown", "sending"):
        problems.append("an earlier send attempt has an unknown outcome: use 'Check Gmail' first")
    attachment, content_hash = None, None
    try:
        _, attachment, content_hash = build_message(settings, draft, None)
    except GmailError as exc:
        problems.append(str(exc))
    return {"to": draft.to_address, "subject": draft.subject, "attachment": attachment, "content_hash": content_hash,
            "problems": problems}


def send_via_gmail(session: Session, settings: Settings, draft: EmailDraft, client: GmailClient, confirm: str,
                   expected_hash: str | None = None) -> dict:
    """Send one approved draft through Gmail. Needs ``confirm == "SEND"``; never retried automatically."""
    if confirm != "SEND":
        raise GmailError("Explicit confirmation required")
    _guard(session, draft)
    reserved = reserved_recipient(draft.to_address)
    if reserved:
        raise GmailError(f"Not sending: {reserved}")
    unverified = unverified_recipient(draft)
    if unverified:
        raise GmailError(f"Not sending: {unverified}")
    if draft.gmail_status in ("send_unknown", "sending"):
        raise GmailError("An earlier send attempt for this draft has an unknown outcome. Use 'Check Gmail' to find out "
                         "whether it was sent before trying again.")
    limit = settings_store.get(session, "outreach")["daily_send_limit"]
    if sent_today_via_gmail(session) >= limit:
        raise GmailError(f"Daily send limit reached ({limit}). JobPilot does not do bulk outreach.")
    created = create_gmail_draft(session, settings, draft, client)
    if expected_hash and expected_hash != draft.gmail_content_hash:
        raise GmailError("The draft changed after you reviewed it; review the send screen again.")
    draft.gmail_send_attempted_at = utcnow()
    _set_state(draft, "sending")
    session.commit()  # an interrupted send is on record and will not be repeated blindly
    try:
        res = client.send_draft(created["id"])
    except GmailNetworkError as exc:
        if not exc.ambiguous:
            _set_state(draft, "created", f"{exc}. Not sent.")
            session.commit()
            raise GmailError(draft.gmail_error) from exc
        _set_state(draft, "send_unknown", f"{exc}. Gmail may or may not have sent it; JobPilot will not retry. "
                                          "Use 'Check Gmail' to find out.")
        session.commit()
        raise GmailError(draft.gmail_error) from exc
    except GmailAPIError as exc:
        ambiguous = exc.status >= 500
        _set_state(draft, "send_unknown" if ambiguous else "created",
                   f"{exc}." + (" The outcome is unknown; use 'Check Gmail'." if ambiguous else " Not sent."))
        session.commit()
        raise GmailError(draft.gmail_error) from exc
    except GmailError as exc:
        _set_state(draft, "created", f"{exc}. Not sent.")
        session.commit()
        raise
    if not res.get("id"):
        _set_state(draft, "send_unknown", "Gmail answered without a message id; use 'Check Gmail'.")
        session.commit()
        raise GmailError(draft.gmail_error)
    _record_gmail_sent(session, draft, res)
    return res


def reconcile(session: Session, settings: Settings, draft: EmailDraft, client: GmailClient) -> dict[str, Any]:
    """Ask Gmail what happened to this draft's Gmail copy (read-only on Gmail; never sends or creates)."""
    if draft.gmail_status == "sent" and draft.status == "sent":
        return {"state": "sent", "message": "Already recorded as sent via Gmail"}
    sent = _sent_message_in_thread(client, draft, _ts(draft.gmail_send_attempted_at or draft.approved_at))
    if sent:
        _record_gmail_sent(session, draft, sent)
        return {"state": "sent", "message": f"Gmail shows it was sent (message {sent.get('id')})"}
    if draft.gmail_draft_id:
        try:
            client.get_draft(draft.gmail_draft_id, "minimal")
            _set_state(draft, "created" if draft.gmail_content_hash else draft.gmail_status)
            return {"state": "draft", "message": "Still in Gmail Drafts; not sent"}
        except GmailAPIError as exc:
            if exc.status != 404:
                raise
        _set_state(draft, "missing", "The Gmail draft is no longer in Drafts and no sent message was found in its "
                                     "thread (deleted in Gmail?)")
        return {"state": "missing", "message": draft.gmail_error}
    if draft.gmail_status == "pending":
        orphan = _find_orphan_draft(client, draft)
        if orphan:
            draft.gmail_draft_id = orphan["id"]
            _set_state(draft, "created")
            return {"state": "draft", "message": "Found the Gmail draft from the interrupted request"}
        _set_state(draft, None, None)
        return {"state": "none", "message": "No Gmail draft exists for this draft"}
    return {"state": draft.gmail_status or "none", "message": "Nothing in Gmail to check"}


def _eligibility(session: Session) -> dict[str, int]:
    rows = list(session.scalars(select(EmailDraft).where(EmailDraft.status.in_(("approved", "sent")))))
    return {"sent_with_thread": sum(1 for d in rows if d.status == "sent" and d.gmail_thread_id and not d.reply_received_at),
            "sent_reply_known": sum(1 for d in rows if d.status == "sent" and d.reply_received_at),
            "sent_without_gmail_thread": sum(1 for d in rows if d.status == "sent" and not d.gmail_thread_id),
            "approved_in_gmail_drafts": sum(1 for d in rows if d.status == "approved" and d.gmail_draft_id),
            "approved_not_in_gmail": sum(1 for d in rows if d.status == "approved" and not d.gmail_draft_id)}


def sync_replies(session: Session, settings: Settings, client: GmailClient) -> dict:
    """Detect replies on threads of messages sent via Gmail; suppresses follow-ups on reply. Never sends.

    Also checks approved drafts whose Gmail copy may have been sent from Gmail itself. A reply is a message in the
    thread that is not yours (not labelled SENT/DRAFT, sender is not you) and arrived after your message.
    """
    require_scope(settings, SCOPE_METADATA, "Checking for replies")
    me = (_load_token(settings) or {}).get("email", "").lower()
    errors: list[str] = []
    found_sent = 0
    for d in session.scalars(select(EmailDraft).where(EmailDraft.status == "approved", EmailDraft.gmail_thread_id.is_not(None),
                                                      EmailDraft.gmail_status.in_(("created", "send_unknown", "sending")))):
        try:
            if reconcile(session, settings, d, client)["state"] == "sent":
                found_sent += 1
        except GmailError as exc:
            errors.append(f"draft #{d.id}: {exc}")
    checked = replies = failed = bounces = 0
    for d in list(session.scalars(select(EmailDraft).where(EmailDraft.status == "sent", EmailDraft.gmail_thread_id.is_not(None),
                                                           EmailDraft.reply_received_at.is_(None)))):
        if d.reply_received_at:  # set meanwhile by a reply on another message of the same thread
            continue
        checked += 1
        try:
            t = client.thread(d.gmail_thread_id)
        except GmailError as exc:
            errors.append(f"draft #{d.id}: {exc}")
            failed += 1
            continue
        sent_ts = _ts(d.sent_at) or 0
        for m in t.get("messages", []):
            labels = set(m.get("labelIds") or [])
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            sender = parseaddr(headers.get("from", ""))[1].lower()
            ts = int(m.get("internalDate", 0)) / 1000
            if sender and sender != me and not labels & {"SENT", "DRAFT"} and ts > sent_ts:
                if is_bounce(sender, headers.get("subject", "")):
                    # a delivery failure is not a reply: the address is marked bounced and blocked for outreach
                    if d.contact is not None:
                        contact_svc.record_bounce(session, d.contact, f"Gmail message {m.get('id', '?')} from {sender}")
                    d.follow_up_due_at, d.suppressed_reason = None, "message bounced"
                    bounces += 1
                    break
                draft_svc.record_reply(session, d, f"Reply from {sender} (detected via Gmail, message {m.get('id', '?')})")
                replies += 1
                break
    if errors and checked == failed and not found_sent:  # every Gmail request failed: not an empty result
        raise GmailError("Reply check failed: " + "; ".join(errors[:3]))
    elig = _eligibility(session)
    message = f"{replies} repl(ies) found in {checked} thread(s)" + (f"; {bounces} bounced" if bounces else "")
    if found_sent:
        message += f"; {found_sent} draft(s) found sent from Gmail"
    if not checked:
        message += (f" — nothing to check: {elig['sent_with_thread']} sent message(s) awaiting a reply have a Gmail "
                    f"thread ({elig['sent_without_gmail_thread']} sent without Gmail, "
                    f"{elig['approved_not_in_gmail']} approved but not in Gmail, "
                    f"{elig['approved_in_gmail_drafts']} in Gmail Drafts not yet sent)")
    if errors:
        message += f"; {len(errors)} error(s): " + "; ".join(errors[:3])
    return {"checked": checked, "replies": replies, "bounces": bounces, "found_sent": found_sent, "errors": errors,
            "eligibility": elig, "message": message}


def inspect_gmail_draft(client: GmailClient, draft_id: str) -> dict[str, Any]:
    """What Gmail itself holds for a draft: headers, labels, body size and attachments (read back from Gmail)."""
    got = client.get_draft(draft_id, "full")
    msg = got.get("message") or {}
    payload = msg.get("payload") or {}
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    attachments = []

    def walk(part: dict) -> None:
        if part.get("filename"):
            attachments.append({"filename": part["filename"], "mime_type": part.get("mimeType"),
                                "size": (part.get("body") or {}).get("size")})
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    body = _payload_text(payload) if not payload.get("filename") else ""
    return {"draft_id": got.get("id"), "message_id": msg.get("id"), "thread_id": msg.get("threadId"),
            "labels": msg.get("labelIds") or [], "to": headers.get("To"), "subject": headers.get("Subject"),
            "body_chars": len(body), "body_first_line": body.strip().splitlines()[0][:120] if body.strip() else "",
            "attachments": attachments}


_BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|mailerdaemon)@", re.I)
_BOUNCE_SUBJECT = re.compile(r"delivery status notification \(failure\)|undeliverable|mail delivery (failed|subsystem)|"
                             r"returned mail|delivery (has )?failed|address not found", re.I)


def is_bounce(sender: str, subject: str) -> bool:
    """A delivery-failure notice (from the mail system), not a person's reply."""
    return bool(_BOUNCE_SENDER.match(sender or "")) or bool(_BOUNCE_SUBJECT.search(subject or ""))


CONFIRM_SUBJECT = re.compile(
    r"thank(?:s| you) for (?:applying|your application|submitting)|application (?:received|submitted|confirmation)|"
    r"we(?:'ve|’ve| have) received your application|received your application|your application (?:to|for|at|with)\b"
    r"[^\n]{0,80}\b(?:has been|was) (?:received|submitted)", re.I)
NOT_CONFIRM = re.compile(r"unfortunately|not (?:be )?moving forward|regret|decided to|other candidates|"
                         r"update on your application|status of your application|interview|assessment", re.I)
# Where ATS confirmation emails come from (plus the employer's own domain, from the job's URLs).
ATS_MAIL_DOMAINS = ("greenhouse-mail.io", "greenhouse.io", "lever.co", "hire.lever.co", "myworkday.com",
                    "myworkdayjobs.com", "ashbyhq.com", "icims.com", "amazon.jobs", "smartrecruiters.com",
                    "successfactors.com", "taleo.net", "eightfold.ai", "phenom.com")
CONFIRM_SEEN_KEY = "gmail.confirmations.seen"
OPENED_RUN_STATES = ("filled", "paused", "closed", "unsupported", "submitting", "uncertain", "confirmed", "submit_failed")


def _registrable(host: str | None) -> str | None:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else None


def detect_confirmation_emails(session: Session, settings: Settings, client: GmailClient,
                               max_messages: int = 50) -> dict:
    """Record a submission when the employer's confirmation email arrives (headers only; the body is never read).

    Considered: applications not yet recorded as applied whose form JobPilot opened (assisted or automatic attempt).
    A message counts only if its subject is confirmation wording (and not a rejection or update), its sender's domain
    is an applicant-tracking mail domain or the employer's own, the company is named, it arrived after the form was
    opened, and exactly one application matches. Each message is looked at once.
    """
    from datetime import timezone
    from email.utils import parsedate_to_datetime
    from urllib.parse import urlsplit

    from jobpilot.models import BrowserRun
    from jobpilot.services import applications as app_svc
    from jobpilot.services import notifications
    from jobpilot.services.text_utils import normalize_company

    require_scope(settings, SCOPE_METADATA, "Checking for application confirmation emails")
    candidates = []
    for app in session.scalars(select(Application).where(Application.applied_at.is_(None),
                                                         Application.status.in_(("preparing", "ready_for_review")))):
        # Only runs that actually showed the form count ("failed" = refused before opening, e.g. site not approved).
        first = session.scalar(select(func.min(BrowserRun.started_at)).where(
            BrowserRun.application_id == app.id, BrowserRun.status.in_(OPENED_RUN_STATES)))
        if first is None:
            continue  # JobPilot never opened its form: nothing to confirm
        domains = {d for d in (_registrable(urlsplit(u or "").hostname) for u in
                               (app.apply_url, app.job.apply_url, app.job.source_url)) if d}
        candidates.append((app, first, normalize_company(app.job.company_name), domains))
    if not candidates:
        return {"checked": 0, "recorded": 0, "message": "no opened, unrecorded applications to confirm"}
    seen = list((notifications.get_state(session, CONFIRM_SEEN_KEY) or {}).get("ids") or [])
    checked = recorded = ambiguous = 0
    for m in client.recent_inbox(max_messages):
        if m["id"] in seen:
            continue
        seen.append(m["id"])
        checked += 1
        h = client.message_headers(m["id"])
        subject, sender = h.get("Subject", ""), h.get("From", "")
        if not CONFIRM_SUBJECT.search(subject) or NOT_CONFIRM.search(subject):
            continue
        name, addr = parseaddr(sender)
        sender_domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
        try:
            when = parsedate_to_datetime(h.get("Date", "")).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        text = f" {normalize_company(f'{subject} {name}')} "
        hits = [c for c in candidates if c[2] and f" {c[2]} " in text and when >= c[1]
                and (sender_domain.endswith(ATS_MAIL_DOMAINS) or any(sender_domain.endswith(d) for d in c[3]))]
        if len(hits) != 1:
            ambiguous += bool(hits)
            continue
        app = hits[0][0]
        app_svc.record_confirmation_email(session, app, when=when, evidence=(
            f"[detected automatically from Gmail message headers; message id {m['id']}]\n"
            f"From: {sender}\nSubject: {subject}\nDate: {h.get('Date', '')}"))
        candidates = [c for c in candidates if c[0].id != app.id]
        recorded += 1
    notifications.set_state(session, CONFIRM_SEEN_KEY, {"ids": seen[-500:]})
    return {"checked": checked, "recorded": recorded, "ambiguous": ambiguous,
            "message": f"{recorded} submission(s) confirmed by email from {checked} new message(s)"
                       + (f"; {ambiguous} ambiguous, left for you" if ambiguous else "")}


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
