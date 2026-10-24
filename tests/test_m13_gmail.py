"""Milestone 13: Gmail draft-first workflow — local vs Gmail state, attachments, safe sending, reply sync.

All Google endpoints are faked in-process (a stateful fake Gmail); no network, no real account. Synthetic data only.
"""
from __future__ import annotations

import base64
import email
import email.policy
import json
import time

import pytest
import requests
from sqlalchemy import select

from jobpilot.integrations import gmail
from jobpilot.models import ContactInteraction, EmailDraft
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from jobpilot.services import resumes as rs
from tests.helpers import make_pdf_bytes, seed_profile

ME = "test.candidate@example.com"


class FakeGmail(requests.Session):
    """A small stateful Gmail: drafts, threads and sent messages, with injectable failures per (method, path)."""

    def __init__(self, scope="https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"):
        super().__init__()
        self.calls: list[tuple[str, str, dict]] = []
        self.drafts: dict[str, dict] = {}
        self.threads: dict[str, list[dict]] = {}
        self.fail: dict[tuple[str, str], list] = {}  # (method, path suffix) -> queue of exceptions/(status, body)
        self.scope = scope
        self.n = 0

    def _resp(self, status, body):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        return r

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        path = url.split("/users/me", 1)[-1] if "/users/me" in url else url
        for (m, suffix), queue in self.fail.items():
            if m == method and path.endswith(suffix) and queue:
                item = queue.pop(0)
                if isinstance(item, tuple) and len(item) == 3:  # (status, body, also_apply)
                    status, body, apply = item
                    if apply:
                        self._handle(method, path, kw)
                    return self._resp(status, body)
                if isinstance(item, tuple):
                    return self._resp(*item)
                if isinstance(item, BaseException) and getattr(item, "_apply_first", False):
                    self._handle(method, path, kw)
                raise item
        return self._handle(method, path, kw)

    def _handle(self, method, path, kw):
        if path.endswith("/token"):
            return self._resp(200, {"access_token": "at", "expires_in": 3600, "refresh_token": "rt", "scope": self.scope})
        if path == "/profile":
            return self._resp(200, {"emailAddress": ME})
        if path == "/drafts" and method == "POST":
            self.n += 1
            did, mid = f"d-{self.n}", f"m-{self.n}"
            tid = kw["json"]["message"].get("threadId") or f"t-{self.n}"
            msg = {"id": mid, "threadId": tid, "labelIds": ["DRAFT"], "raw": kw["json"]["message"]["raw"],
                   "internalDate": str(int(time.time() * 1000))}
            self.drafts[did] = msg
            self.threads.setdefault(tid, []).append(msg)
            return self._resp(200, {"id": did, "message": {"id": mid, "threadId": tid}})
        if path == "/drafts" and method == "GET":
            return self._resp(200, {"drafts": [{"id": k, "message": {"id": v["id"], "threadId": v["threadId"]}}
                                               for k, v in self.drafts.items()]})
        if path.startswith("/drafts/") and method == "GET":
            did = path.split("/")[-1]
            if did not in self.drafts:
                return self._resp(404, {"error": {"code": 404, "message": "Requested entity was not found."}})
            msg = self.drafts[did]
            parsed = email.message_from_bytes(base64.urlsafe_b64decode(msg["raw"] + "=="), policy=email.policy.default)
            headers = [{"name": k, "value": str(v)} for k, v in parsed.items()]
            return self._resp(200, {"id": did, "message": {"id": msg["id"], "threadId": msg["threadId"],
                                                           "labelIds": msg["labelIds"], "payload": {"headers": headers}}})
        if path == "/drafts/send":
            did = kw["json"]["id"]
            msg = self.drafts.pop(did)
            msg["labelIds"] = ["SENT"]
            msg["internalDate"] = str(int(time.time() * 1000))
            return self._resp(200, {"id": msg["id"], "threadId": msg["threadId"], "labelIds": ["SENT"]})
        if path.startswith("/threads/"):
            tid = path.split("/")[-1]
            msgs = [{"id": m["id"], "labelIds": m["labelIds"], "internalDate": m["internalDate"],
                     "payload": {"headers": m.get("headers") or [{"name": "From", "value": f"Me <{ME}>"}]}}
                    for m in self.threads.get(tid, [])]
            return self._resp(200, {"id": tid, "messages": msgs})
        return self._resp(404, {"error": {"message": f"unhandled {method} {path}"}})

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def reply(self, tid, sender="jane@examplecorp.org", delay=60):
        self.threads[tid].append({"id": f"r-{len(self.threads[tid])}", "labelIds": ["INBOX", "UNREAD"],
                                  "internalDate": str(int((time.time() + delay) * 1000)),
                                  "headers": [{"name": "From", "value": f"Jane <{sender}>"}]})

    def count(self, method, suffix):
        return sum(1 for m, u, _ in self.calls if m == method and u.endswith(suffix))


@pytest.fixture
def gs(settings):
    settings.gmail_enabled = True
    settings.ensure_dirs()
    settings.gmail_secrets_file.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    settings.gmail_token_file.write_text(json.dumps({
        "access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600, "email": ME,
        "scope": "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.metadata"}))
    return settings


@pytest.fixture
def fake():
    return FakeGmail()


def client(settings, fake):
    return gmail.GmailClient(settings, http=fake)


def _resume(db, settings):
    from jobpilot.models import Resume

    master = db.scalar(select(Resume).where(Resume.is_master.is_(True))) or rs.create_resume(db, "Master resume", "master", True)
    existing = next((v for v in master.versions if v.original_filename == "Candidate_Resume.pdf"), None)
    if existing:
        return existing
    v = rs.import_version(db, settings, master, "Candidate_Resume.pdf", make_pdf_bytes(["Test Candidate", "Java"]))
    rs.approve_version(db, v, acknowledged=True)
    return v


def _draft(db, settings, to="jane@examplecorp.org", resume=True, approve=True, body=None, verified=True):
    from jobpilot.services import profile as ps

    if ps.get_profile(db).id is None:
        seed_profile(db)
    v = _resume(db, settings) if resume else None
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email=to, company_name="Examplecorp"))
    if verified:
        cs.verify_email(db, c, "synthetic test: address confirmed")  # sending requires a verified recipient
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id,
                                           resume_version_id=v.id if v else None))
    text = body or ("Hi Jane,\n\nSynthetic outreach text." + (" My resume is attached." if resume else ""))
    ds.edit_draft(db, d, ds.DraftEdit(to_address=to, subject="Backend role (synthetic)", body=text))
    if approve:
        ds.approve(db, d)
    db.commit()
    return d


def _mime(fake):
    raw = next(kw["json"]["message"]["raw"] for m, u, kw in reversed(fake.calls) if m == "POST" and u.endswith("/drafts"))
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=="), policy=email.policy.default)


# ---- Local drafts ------------------------------------------------------------------------------------------

def test_local_draft_create_edit_approve_never_touch_gmail(db, gs, fake):
    d = _draft(db, gs, approve=False)
    assert d.status == "draft" and d.gmail_status is None
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject="Edited subject", body=d.body))
    assert d.subject == "Edited subject"
    ds.approve(db, d)
    assert d.status == "approved" and d.gmail_draft_id is None and d.gmail_status is None
    assert d.sent_at is None and not fake.calls, "approval is local only: Gmail was not contacted, nothing was sent"


def test_web_approve_message_is_accurate_and_status_panel_separates_states(client_web, db, gs):
    d = _draft(db, gs, approve=False)
    r = client_web.post(f"/drafts/{d.id}/approve")
    assert "Nothing+was+sent" in r.headers["location"] and "no+Gmail+draft" in r.headers["location"]
    page = client_web.get(f"/drafts/{d.id}").text
    for needle in ("Local draft", "Approval", "Gmail", "Sent", "Resume attachment", "no Gmail draft", "not sent",
                   "attached only when the Gmail draft is created", "Create Gmail draft"):
        assert needle in page, needle


def test_draft_page_explains_why_gmail_is_unavailable(client_web, db, settings):
    d = _draft(db, settings)
    page = client_web.get(f"/drafts/{d.id}").text
    assert "Gmail actions are unavailable in this server" in page and "JOBPILOT_GMAIL_ENABLED" in page
    assert "Create Gmail draft</button>" not in page


# ---- Creating the Gmail draft ------------------------------------------------------------------------------

def test_create_gmail_draft_builds_correct_mime_with_the_selected_resume(db, gs, fake):
    d = _draft(db, gs)
    res = gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert res == {"id": "d-1", "reused": False, "attachment": d.gmail_attachment}
    m = _mime(fake)
    assert m["To"] == "jane@examplecorp.org" and m["Subject"] == "Backend role (synthetic)" and m["From"] == ME
    assert m.get_content_type() == "multipart/mixed"
    text = m.get_body(preferencelist=("plain",)).get_content()
    assert "Synthetic outreach text. My resume is attached." in text
    (att,) = list(m.iter_attachments())
    expected = (gs.files_dir / d.resume_version.file_path).read_bytes()
    assert att.get_filename() == "Candidate_Resume.pdf" and att.get_content_type() == "application/pdf"
    assert att.get_content() == expected, "the exact stored bytes of the draft's own resume version"
    assert d.gmail_status == "created" and d.gmail_draft_id == "d-1" and d.gmail_thread_id == "t-1"
    assert d.gmail_attachment["filename"] == "Candidate_Resume.pdf" and d.gmail_attachment["size"] == len(expected)
    assert d.status == "approved" and d.sent_at is None and fake.count("POST", "/drafts/send") == 0


def test_attachment_uses_the_drafts_resume_version_not_the_latest(db, gs, fake):
    d = _draft(db, gs)
    first = d.resume_version
    newer = rs.import_version(db, gs, first.resume, "Newer.pdf", make_pdf_bytes(["Test Candidate", "Newer"]))
    rs.approve_version(db, newer, acknowledged=True)
    gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    (att,) = list(_mime(fake).iter_attachments())
    assert att.get_filename() == "Candidate_Resume.pdf"


def test_persisted_gmail_draft_id_is_reused_not_duplicated(db, gs, fake):
    d = _draft(db, gs)
    gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    again = gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert again["reused"] and again["id"] == "d-1" and fake.count("POST", "/drafts") == 1
    db.expire_all()
    assert db.get(EmailDraft, d.id).gmail_draft_id == "d-1", "persisted"


def test_missing_recipient_and_unapproved_drafts_never_reach_gmail(db, gs, fake):
    d = _draft(db, gs, approve=False)
    with pytest.raises(gmail.GmailError, match="approve"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    d.to_address = None
    d.status = "approved"
    with pytest.raises(gmail.GmailError, match="recipient"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert not fake.calls


def test_missing_resume_file_fails_clearly_and_claims_nothing(db, gs, fake):
    d = _draft(db, gs)
    (gs.files_dir / d.resume_version.file_path).unlink()
    with pytest.raises(gmail.GmailError, match="missing"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_draft_id is None and d.gmail_attachment is None and not fake.calls


def test_body_claiming_an_attachment_without_a_resume_is_refused(db, gs, fake):
    d = _draft(db, gs, resume=False, body="Hi, my resume is attached for your review.")
    with pytest.raises(gmail.GmailError, match="says a resume is attached"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    d2 = _draft(db, gs, to="ann@examplecorp.org", resume=False, body="Hi, a short note without attachments.")
    gmail.create_gmail_draft(db, gs, d2, client(gs, fake))
    assert not list(_mime(fake).iter_attachments()) and d2.gmail_attachment is None


def test_insufficient_scope_gives_actionable_error(db, gs, fake):
    d = _draft(db, gs)
    tok = json.loads(gs.gmail_token_file.read_text())
    tok["scope"] = "https://www.googleapis.com/auth/gmail.metadata"
    gs.gmail_token_file.write_text(json.dumps(tok))
    with pytest.raises(gmail.GmailScopeError, match="gmail auth"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert not gmail.readiness(gs)["ready"]
    # Gmail itself refusing for scope reasons is classified the same way.
    tok["scope"] = "https://www.googleapis.com/auth/gmail.compose"
    gs.gmail_token_file.write_text(json.dumps(tok))
    fake.fail[("POST", "/drafts")] = [(403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                                       "status": "PERMISSION_DENIED"}})]
    with pytest.raises(gmail.GmailScopeError):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_status == "failed" and "insufficient" in d.gmail_error


def test_revoked_token_gives_reauthorisation_error(db, gs, fake):
    d = _draft(db, gs)
    tok = json.loads(gs.gmail_token_file.read_text())
    tok["expires_at"] = 0
    gs.gmail_token_file.write_text(json.dumps(tok))
    fake.fail[("POST", "/token")] = [(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})]
    with pytest.raises(gmail.GmailAuthError, match="gmail auth"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))


def test_gmail_api_error_is_reported_not_swallowed(db, gs, fake, client_web):
    d = _draft(db, gs)
    fake.fail[("POST", "/drafts")] = [(400, {"error": {"code": 400, "message": "Invalid To header"}})]
    with pytest.raises(gmail.GmailAPIError, match="Invalid To header"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_status == "failed" and d.gmail_draft_id is None
    db.commit()
    assert "Invalid To header" in client_web.get(f"/drafts/{d.id}").text


def test_ambiguous_create_is_recovered_without_a_duplicate(db, gs, fake):
    d = _draft(db, gs)
    exc = requests.ReadTimeout("read timed out")
    exc._apply_first = True  # Gmail created the draft, but the answer was lost
    fake.fail[("POST", "/drafts")] = [exc]
    with pytest.raises(gmail.GmailError, match="may have created"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_status == "pending" and d.gmail_draft_id is None
    res = gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert res["reused"] and res["id"] == "d-1" and len(fake.drafts) == 1, "found in Gmail Drafts, not created twice"


def test_definite_network_failure_marks_failed_and_retry_creates(db, gs, fake):
    d = _draft(db, gs)
    fake.fail[("POST", "/drafts")] = [requests.ConnectTimeout("connect timeout")]
    with pytest.raises(gmail.GmailError, match="No draft was created"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_status == "failed"
    assert not gmail.create_gmail_draft(db, gs, d, client(gs, fake))["reused"] and len(fake.drafts) == 1


def test_editing_marks_the_gmail_copy_outdated(db, gs, fake):
    d = _draft(db, gs)
    gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    ds.edit_draft(db, d, ds.DraftEdit(to_address=d.to_address, subject=d.subject, body="New text (synthetic). My resume is attached."))
    assert d.gmail_status == "outdated" and d.gmail_stale_draft_id == "d-1" and d.gmail_draft_id is None


# ---- Sending -------------------------------------------------------------------------------------------------

def test_send_requires_explicit_confirmation_and_records_gmail_ids(db, gs, fake):
    d = _draft(db, gs)
    with pytest.raises(gmail.GmailError, match="confirmation"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "yes")
    assert fake.count("POST", "/drafts/send") == 0
    gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert fake.count("POST", "/drafts/send") == 1
    assert d.status == "sent" and d.sent_via == "gmail" and d.gmail_status == "sent"
    assert d.gmail_message_id == "m-1" and d.sent_at is not None and d.follow_up_due_at is not None


def test_reserved_placeholder_recipient_can_be_drafted_but_never_sent(db, gs, fake):
    d = _draft(db, gs, to="recruiter@mastercard.example")
    gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_status == "created", "a draft for review is harmless"
    with pytest.raises(gmail.GmailError, match="reserved placeholder"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert fake.count("POST", "/drafts/send") == 0 and d.status == "approved"
    assert gmail.reserved_recipient("a@b.invalid") and gmail.reserved_recipient("x@localhost")
    assert gmail.reserved_recipient("jane@examplecorp.org") is None


def test_unverified_or_unlinked_recipient_is_never_sent_to(db, gs, fake):
    d = _draft(db, gs, to="ann@examplecorp.org", verified=False)
    gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert d.gmail_status == "created", "drafting for review is allowed"
    with pytest.raises(gmail.GmailError, match="verify it"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert "verify it" in " ".join(gmail.send_preview(db, gs, d)["problems"])
    typed = _draft(db, gs, to="bob@examplecorp.org")  # verified contact, but the To field is changed by hand
    ds.edit_draft(db, typed, ds.DraftEdit(to_address="someone.else@examplecorp.org", subject=typed.subject, body=typed.body))
    ds.approve(db, typed)
    with pytest.raises(gmail.GmailError, match="not linked to a contact"):
        gmail.send_via_gmail(db, gs, typed, client(gs, fake), "SEND")
    assert fake.count("POST", "/drafts/send") == 0


def test_send_preview_and_confirmation_page(client_web, db, gs, fake, monkeypatch):
    d = _draft(db, gs)
    page = client_web.get(f"/gmail/drafts/{d.id}/send").text
    assert "jane@examplecorp.org" in page and "Candidate_Resume.pdf" in page and "Send now via Gmail" in page
    assert not fake.calls, "the confirmation screen contacts nothing"
    monkeypatch.setattr(gmail.requests, "Session", lambda: fake)
    r = client_web.post(f"/gmail/drafts/{d.id}/send", data={"confirm": "SEND"})
    assert "Tick" in r.headers["location"].replace("+", " ") and fake.count("POST", "/drafts/send") == 0
    preview = gmail.send_preview(db, gs, d)
    r = client_web.post(f"/gmail/drafts/{d.id}/send", data={"confirm": "SEND", "ack": "1", "content_hash": "stale"})
    assert "changed" in r.headers["location"] and fake.count("POST", "/drafts/send") == 0
    r = client_web.post(f"/gmail/drafts/{d.id}/send", data={"confirm": "SEND", "ack": "1",
                                                            "content_hash": preview["content_hash"]})
    assert "confirmed+the+message+was+sent" in r.headers["location"] and fake.count("POST", "/drafts/send") == 1
    reserved = _draft(db, gs, to="recruiter@mastercard.example")
    assert "Cannot send" in client_web.get(f"/gmail/drafts/{reserved.id}/send").text


def test_manual_mark_sent_does_not_claim_a_gmail_send(db, gs, fake, client_web):
    d = _draft(db, gs)
    r = client_web.post(f"/drafts/{d.id}/sent")
    assert "manual+record" in r.headers["location"]
    db.expire_all()
    d = db.get(EmailDraft, d.id)
    assert d.status == "sent" and d.sent_via == "manual" and d.gmail_message_id is None and d.gmail_status is None
    page = client_web.get(f"/drafts/{d.id}").text
    assert "recorded by you" in page and "not verified with Gmail" in page and "sent via Gmail" not in page
    assert not fake.calls


def test_ambiguous_send_is_never_retried_blindly_and_check_resolves_it(db, gs, fake):
    d = _draft(db, gs)
    exc = requests.ReadTimeout("read timed out")
    exc._apply_first = True  # Gmail sent it, but the answer was lost
    fake.fail[("POST", "/drafts/send")] = [exc]
    with pytest.raises(gmail.GmailError, match="will not retry"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert d.gmail_status == "send_unknown" and d.status == "approved"
    with pytest.raises(gmail.GmailError, match="unknown outcome"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert fake.count("POST", "/drafts/send") == 1, "no second send"
    res = gmail.reconcile(db, gs, d, client(gs, fake))
    assert res["state"] == "sent" and d.status == "sent" and d.sent_via == "gmail" and d.gmail_message_id == "m-1"


def test_ambiguous_send_that_did_not_happen_returns_to_draft_state(db, gs, fake):
    d = _draft(db, gs)
    fake.fail[("POST", "/drafts/send")] = [(503, {"error": {"message": "Backend error"}})]
    with pytest.raises(gmail.GmailError, match="unknown"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert d.gmail_status == "send_unknown"
    assert gmail.reconcile(db, gs, d, client(gs, fake))["state"] == "draft"
    assert d.gmail_status == "created" and d.status == "approved"


def test_definite_send_rejection_is_not_marked_sent(db, gs, fake):
    d = _draft(db, gs)
    fake.fail[("POST", "/drafts/send")] = [(400, {"error": {"message": "Invalid recipient"}})]
    with pytest.raises(gmail.GmailError, match="Not sent"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert d.status == "approved" and d.sent_at is None and d.gmail_status == "created"


def test_daily_limit_and_dnc_still_apply(db, gs, fake):
    from jobpilot.services import settings_store

    settings_store.put(db, "outreach", {"follow_up_days": 7, "max_follow_ups": 2, "daily_send_limit": 0})
    d = _draft(db, gs)
    with pytest.raises(gmail.GmailError, match="limit"):
        gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    cs.add_dnc(db, cs.DncIn(kind="email", value=d.to_address))
    db.refresh(d)
    with pytest.raises(gmail.GmailError, match="do-not-contact|blocked|list"):
        gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    assert not fake.count("POST", "/drafts/send")


# ---- Reply synchronisation ------------------------------------------------------------------------------------

def test_sync_with_nothing_eligible_says_why(db, gs, fake):
    d = _draft(db, gs)  # approved, not in Gmail
    res = gmail.sync_replies(db, gs, client(gs, fake))
    assert res["checked"] == 0 and res["errors"] == [] and res["eligibility"]["approved_not_in_gmail"] == 1
    assert "nothing to check" in res["message"] and "1 approved but not in Gmail" in res["message"]
    assert not [u for m, u, _ in fake.calls if "/threads/" in u]
    ds.mark_sent(db, d)  # manual: no Gmail thread, so not checkable
    assert gmail.sync_replies(db, gs, client(gs, fake))["eligibility"]["sent_without_gmail_thread"] == 1


def test_sync_detects_reply_once_and_suppresses_followups(db, gs, fake):
    d = _draft(db, gs)
    gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    assert gmail.sync_replies(db, gs, client(gs, fake))["replies"] == 0, "your own sent message is not a reply"
    fake.reply(d.gmail_thread_id)
    res = gmail.sync_replies(db, gs, client(gs, fake))
    assert res["checked"] == 1 and res["replies"] == 1
    assert d.reply_received_at is not None and d.follow_up_due_at is None and d.suppressed_reason == "reply received"
    with pytest.raises(ds.DraftError, match="reply"):
        ds.create_followup(db, d)
    again = gmail.sync_replies(db, gs, client(gs, fake))
    assert again["checked"] == 0 and again["replies"] == 0
    inbound = db.scalars(select(ContactInteraction).where(ContactInteraction.direction == "inbound")).all()
    assert len(inbound) == 1, "no duplicate reply records"


def test_sync_reports_api_failure_instead_of_empty_result(db, gs, fake):
    d = _draft(db, gs)
    gmail.send_via_gmail(db, gs, d, client(gs, fake), "SEND")
    fake.fail[("GET", d.gmail_thread_id)] = [(500, {"error": {"message": "Backend error"}})]
    with pytest.raises(gmail.GmailError, match="Reply check failed"):
        gmail.sync_replies(db, gs, client(gs, fake))


def test_sync_finds_drafts_sent_from_gmail_itself(db, gs, fake):
    d = _draft(db, gs)
    gmail.create_gmail_draft(db, gs, d, client(gs, fake))
    msg = fake.drafts.pop("d-1")  # you pressed Send in Gmail
    msg["labelIds"] = ["SENT"]
    res = gmail.sync_replies(db, gs, client(gs, fake))
    assert res["found_sent"] == 1 and d.status == "sent" and d.sent_via == "gmail" and d.gmail_message_id == "m-1"
    assert fake.count("POST", "/drafts/send") == 0, "JobPilot did not send it"


# ---- CLI ---------------------------------------------------------------------------------------------------------

def test_cli_test_draft_creates_labelled_draft_without_sending(gs, fake, monkeypatch, capsys):
    from jobpilot import cli

    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(gs.data_dir))
    monkeypatch.setenv("JOBPILOT_GMAIL_ENABLED", "true")
    from jobpilot.config import get_settings

    get_settings.cache_clear()  # settings are cached per process; other CLI tests ran with Gmail disabled
    assert cli.main(["init"]) == 0
    monkeypatch.setattr(gmail.requests, "Session", lambda: fake)
    capsys.readouterr()
    assert cli.main(["gmail", "test-draft"]) == 1
    assert cli.main(["gmail", "test-draft", "--to", "jobpilot-test@example.com"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["created"] and out["gmail_status"] == "created" and out["gmail_draft_id"] == "d-1"
    assert "do not send" in out["subject"] and out["sent_at"] is None
    assert cli.main(["gmail", "inspect", "--draft", str(out["draft"])]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["to"] == "jobpilot-test@example.com" and info["labels"] == ["DRAFT"]
    assert cli.main(["gmail", "test-draft", "--to", "x@example.com", "--resume-version", "999"]) == 1
    assert fake.count("POST", "/drafts/send") == 0
    get_settings.cache_clear()


@pytest.fixture
def client_web(app):
    from fastapi.testclient import TestClient

    return TestClient(app, follow_redirects=False)
