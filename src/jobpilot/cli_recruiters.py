"""CLI for recruiters and outreach.

``jobpilot recruiters add|import|list|search|enrich`` and ``jobpilot outreach draft|approve|list``.
Nothing here sends email: an approved draft goes to Gmail Drafts with ``jobpilot gmail create-draft --draft ID``.
LinkedIn is never browsed or scraped: profile URLs are what you paste in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _out(data) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _contact_row(s, c) -> dict:
    from jobpilot.services import contacts as contact_svc

    return {"id": c.id, "name": c.name, "title": c.title, "company": c.company.name if c.company else None,
            "email": c.email, "email_status": c.email_status or ("no email" if not c.email else None),
            "linkedin": c.linkedin_url, "source": c.source_type, "do_not_contact": c.do_not_contact,
            "jobs": [j.id for j, _, _ in contact_svc.jobs_for_contact(s, c)],
            "warning": contact_svc.domain_warning(c)}


def cmd_recruiters(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.db import session_scope
    from jobpilot.models import Contact, Job
    from jobpilot.services import apollo
    from jobpilot.services import contacts as contact_svc

    settings, _, factory = _ctx()
    try:
        if args.action == "add":
            source = {"linkedin": "linkedin_manual", "apollo": "apollo_manual"}.get(args.source, args.source)
            with session_scope(factory) as s:
                c, merged = contact_svc.save_contact(s, contact_svc.ContactIn(
                    name=args.name, title=args.title, company_name=args.company, job_id=args.job,
                    role_type="recruiter", email=args.email,
                    email_status=args.email_status or ("apollo_reported" if source == "apollo_manual" and args.email
                                                       else None),
                    linkedin_url=args.linkedin, source_type=source, source_url=args.source_url,
                    verification_note=args.verification_note, notes=args.notes))
                _out({"merged_into_existing": merged, **_contact_row(s, c)})
        elif args.action == "import":
            with session_scope(factory) as s:
                _out(apollo.import_csv(s, Path(args.file).read_bytes(), job_id=args.job))
        elif args.action == "update":
            with session_scope(factory) as s:
                c = s.get(Contact, args.id or 0)
                if c is None:
                    raise LookupError("Contact not found (give --id)")
                current = {"name": c.name, "title": c.title, "company_name": c.company.name if c.company else None,
                           "role_type": c.role_type, "email": c.email, "email_status": c.email_status,
                           "linkedin_url": c.linkedin_url, "source_type": c.source_type, "source_url": c.source_url,
                           "source_note": c.source_note, "verification_note": c.verification_note, "notes": c.notes}
                changes = {k: v for k, v in {"name": args.name, "title": args.title, "company_name": args.company,
                                             "email": args.email, "email_status": args.email_status,
                                             "linkedin_url": args.linkedin, "source_url": args.source_url,
                                             "verification_note": args.verification_note, "notes": args.notes}.items()
                           if v is not None}
                if not changes:
                    raise ValueError("Nothing to change: give the fields to correct (e.g. --name, --linkedin)")
                c, _ = contact_svc.save_contact(s, contact_svc.ContactIn(**{**current, **changes}), contact_id=c.id)
                _out(_contact_row(s, c))
        elif args.action == "remove":
            with session_scope(factory) as s:
                c = s.get(Contact, args.id or 0)
                if c is None:
                    raise LookupError("Contact not found (give --id)")
                if not args.yes:
                    _out({"would_remove": _contact_row(s, c), "next": "repeat with --yes to remove it"})
                    return 0
                _out(contact_svc.delete_contact(s, c, args.reason or ""))
        elif args.action == "list":
            with factory() as s:
                q = s.query(Contact).filter(Contact.role_type == "recruiter")
                rows = [_contact_row(s, c) for c in q.order_by(Contact.id)]
                if args.job:
                    rows = [r for r in rows if args.job in r["jobs"]]
                _out(rows)
        elif args.action == "search":
            with session_scope(factory) as s:
                _out({"credits_used": 0, "results": apollo.search_recruiters(s, settings, args.domain),
                      "note": "search results are candidates, not verified recruiters; enrich one to get a work email"})
        elif args.action == "enrich":
            with session_scope(factory) as s:
                job = s.get(Job, args.job) if args.job else None
                c, what = apollo.enrich_contact(s, settings, args.apollo_id, job=job, spend_credit=args.spend_credit)
                _out({"result": what, "contact": _contact_row(s, c) if c else None})
    except (apollo.ApolloError, ValueError, LookupError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_outreach(args) -> int:
    from jobpilot.cli import _ctx
    from jobpilot.db import session_scope
    from jobpilot.models import EmailDraft
    from jobpilot.services import drafts as draft_svc

    _, _, factory = _ctx()
    try:
        if args.action == "draft":
            if not (args.job and args.contact):
                raise ValueError("outreach draft needs --job ID and --contact ID")
            with session_scope(factory) as s:
                d = draft_svc.create_draft(s, draft_svc.DraftCreate(template_key=args.template, job_id=args.job,
                                                                    contact_id=args.contact,
                                                                    additional_contact_reason=args.reason))
                _out({"draft": d.id, "status": d.status, "to": d.to_address, "subject": d.subject, "body": d.body,
                      "missing": d.missing_fields, "warnings": draft_svc.recipient_warnings(d),
                      "next": f"review it, then `jobpilot outreach approve --draft {d.id}` and "
                              f"`jobpilot gmail create-draft --draft {d.id}` (a Gmail draft; nothing is sent)"})
        elif args.action == "approve":
            with session_scope(factory) as s:
                d = s.get(EmailDraft, args.draft)
                if d is None:
                    raise LookupError("Draft not found")
                draft_svc.approve(s, d, acknowledge_guessed=args.acknowledge_guessed)
                _out({"draft": d.id, "status": d.status, "to": d.to_address, "warnings": draft_svc.recipient_warnings(d)})
        elif args.action == "cancel":
            with session_scope(factory) as s:
                d = s.get(EmailDraft, args.draft or 0)
                if d is None:
                    raise LookupError("Draft not found (give --draft)")
                draft_svc.cancel(s, d)
                _out({"draft": d.id, "status": d.status, "to": d.to_address, "gmail_draft_id": d.gmail_draft_id,
                      "note": "kept with its history; it can no longer be sent or put in Gmail"
                              + ("; a copy is still in Gmail Drafts — delete it there" if d.gmail_draft_id else "")})
        elif args.action == "list":
            with factory() as s:
                _out([{"draft": d.id, "kind": d.kind, "status": d.status, "to": d.to_address, "subject": d.subject,
                       "job": d.job_id, "contact": d.contact_id, "gmail": d.gmail_status, "sent_at": d.sent_at,
                       "reply_at": d.reply_received_at}
                      for d in s.query(EmailDraft).filter(EmailDraft.kind.in_(draft_svc.OUTREACH_KINDS))
                      .order_by(EmailDraft.id)])
    except (draft_svc.DraftError, ValueError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    s = sub.add_parser("recruiters", help="Recruiter contacts: add (LinkedIn/Apollo found by you), update, remove "
                                          "(placeholders only), import an Apollo CSV, list, Apollo API search "
                                          "(0 credits) and enrich (uses a credit)")
    s.add_argument("action", choices=["add", "update", "remove", "import", "list", "search", "enrich"])
    s.add_argument("--id", type=int, help="update/remove: contact id")
    s.add_argument("--reason", help="remove: why (kept in the audit log)")
    s.add_argument("--yes", action="store_true", help="remove: confirm (without it, shows what would be removed)")
    s.add_argument("file", nargs="?", help="import: Apollo CSV export")
    s.add_argument("--job", type=int, help="associate with this job")
    s.add_argument("--name")
    s.add_argument("--title")
    s.add_argument("--company")
    s.add_argument("--email")
    s.add_argument("--email-status", choices=["verified", "apollo_reported", "unverified", "guessed"],
                   help="what you actually know about the address (verified needs --verification-note)")
    s.add_argument("--verification-note")
    s.add_argument("--linkedin", help="profile URL you found yourself (JobPilot never visits LinkedIn)")
    s.add_argument("--source", default="linkedin", choices=["linkedin", "apollo", "company_website", "job_posting",
                                                            "manual"])
    s.add_argument("--source-url")
    s.add_argument("--notes")
    s.add_argument("--domain", help="search: employer web domain, e.g. rubrik.com")
    s.add_argument("--apollo-id", help="enrich: the id from `recruiters search`")
    s.add_argument("--spend-credit", action="store_true", help="enrich: confirm using an Apollo credit")
    s.set_defaults(func=cmd_recruiters)

    o = sub.add_parser("outreach", help="Recruiter emails: draft (local), approve, cancel, list. Never sends.")
    o.add_argument("action", choices=["draft", "approve", "cancel", "list"])
    o.add_argument("--job", type=int)
    o.add_argument("--contact", type=int)
    o.add_argument("--template", default="recruiter_outreach",
                   choices=["recruiter_outreach", "hiring_manager", "referral_request"])
    o.add_argument("--reason", help="draft: why a second person for the same job")
    o.add_argument("--draft", type=int)
    o.add_argument("--acknowledge-guessed", action="store_true", help="approve: you accept a guessed address")
    o.set_defaults(func=cmd_outreach)
