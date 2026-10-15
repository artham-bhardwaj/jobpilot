"""Recruiter directory, do-not-contact list, email templates and the draft center."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Company, Contact, DoNotContact, EmailDraft, EmailTemplate, Job
from jobpilot.services import audit
from jobpilot.services import contacts as cs
from jobpilot.services import drafts as ds
from jobpilot.web.deps import first_error, form_dict, get_db, get_or_404, redirect, render

router = APIRouter()


# ---- Contacts ------------------------------------------------------------------

@router.get("/contacts")
def contacts_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    rows = cs.search(db, q=qp.get("q") or None, role_type=qp.get("role_type") or None,
                     company_id=int(qp["company_id"]) if qp.get("company_id", "").isdigit() else None)
    dnc = list(db.scalars(select(DoNotContact).order_by(DoNotContact.created_at.desc())))
    prefill_job = db.get(Job, int(qp["job_id"])) if qp.get("job_id", "").isdigit() else None
    return render(request, "contacts.html", contacts=rows, dnc=dnc, q=qp.get("q") or "",
                  role_type=qp.get("role_type") or "", prefill_job=prefill_job)


@router.post("/contacts")
@router.post("/contacts/{contact_id:int}")
async def contact_save(request: Request, contact_id: int | None = None, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        contact, merged = cs.save_contact(db, cs.ContactIn.model_validate(form), contact_id)
    except (ValidationError, ValueError, LookupError) as exc:
        back = f"/contacts/{contact_id}" if contact_id else "/contacts"
        return redirect(back, err=first_error(exc))
    msg = "Merged into an existing contact (existing values kept)" if merged else "Contact saved"
    return redirect(f"/contacts/{contact.id}", msg=msg)


@router.get("/contacts/{contact_id:int}")
def contact_detail(contact_id: int, request: Request, db: Session = Depends(get_db)):
    c = get_or_404(db, Contact, contact_id)
    drafts = list(db.scalars(select(EmailDraft).where(EmailDraft.contact_id == c.id).order_by(EmailDraft.id.desc())))
    blocked = cs.is_blocked(db, c.email, c.company_id)
    linked = cs.jobs_for_contact(db, c)
    from jobpilot.models import Application

    linked_ids = [j.id for j, _, _ in linked]
    apps_for = {a.job_id: a for a in db.scalars(select(Application).where(Application.job_id.in_(linked_ids)))} if linked_ids else {}
    candidates = list(db.scalars(select(Job).where(Job.status == "active", Job.id.not_in(linked_ids) if linked_ids else True)
                                 .order_by((Job.company_id == c.company_id).desc(), Job.first_seen_at.desc()).limit(100)))
    return render(request, "contact_detail.html", c=c, drafts=drafts, blocked=blocked, linked=linked,
                  apps_for=apps_for, link_candidates=candidates,
                  templates=_templates(db), kind_labels=ds.KIND_LABELS)


@router.post("/contacts/{contact_id:int}/verify")
async def contact_verify(contact_id: int, request: Request, db: Session = Depends(get_db)):
    c = get_or_404(db, Contact, contact_id)
    form = await form_dict(request)
    try:
        cs.verify_email(db, c, form.get("note") or "")
    except (ValueError, AttributeError) as exc:
        return redirect(f"/contacts/{contact_id}", err=str(exc))
    return redirect(f"/contacts/{contact_id}", msg="Email marked verified")


@router.post("/contacts/{contact_id:int}/dnc")
async def contact_dnc(contact_id: int, request: Request, db: Session = Depends(get_db)):
    c = get_or_404(db, Contact, contact_id)
    form = await form_dict(request)
    cs.set_dnc(db, c, form.get("flag") == "1", form.get("reason"))
    return redirect(f"/contacts/{contact_id}", msg="Do-not-contact updated")


@router.post("/contacts/{contact_id:int}/interaction")
async def contact_interaction(contact_id: int, request: Request, db: Session = Depends(get_db)):
    c = get_or_404(db, Contact, contact_id)
    form = await form_dict(request)
    try:
        cs.log_interaction(db, c, form.get("channel") or "", form.get("direction") or "", form.get("summary") or "")
    except (ValueError, AttributeError) as exc:
        return redirect(f"/contacts/{contact_id}", err=str(exc))
    return redirect(f"/contacts/{contact_id}", msg="Interaction logged")


@router.post("/contacts/{contact_id:int}/delete")
def contact_delete(contact_id: int, db: Session = Depends(get_db)):
    c = get_or_404(db, Contact, contact_id)
    db.delete(c)
    audit.record(db, "contact.deleted", "contact", contact_id)
    return redirect("/contacts", msg="Contact deleted")


@router.get("/contacts/duplicates")
def contact_duplicates(request: Request, db: Session = Depends(get_db)):
    return render(request, "contact_duplicates.html", pairs=cs.duplicate_candidates(db))


@router.post("/contacts/merge")
async def contact_merge(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        keep = get_or_404(db, Contact, int(form.get("keep_id") or 0), "contact")
        other = get_or_404(db, Contact, int(form.get("other_id") or 0), "contact")
        if form.get("confirm") != "1":
            raise ValueError("Tick the confirmation to merge")
        res = cs.merge_contacts(db, keep, other)
    except ValueError as exc:
        return redirect("/contacts/duplicates", err=str(exc))
    note = f"; {len(res['conflicts'])} conflicting field(s) kept from #{keep.id}" if res["conflicts"] else ""
    return redirect(f"/contacts/{keep.id}", msg=f"Merged into #{keep.id}{note}")


@router.post("/contacts/{contact_id:int}/link-job")
async def contact_link_job(contact_id: int, request: Request, db: Session = Depends(get_db)):
    c = get_or_404(db, Contact, contact_id)
    form = await form_dict(request)
    try:
        job = get_or_404(db, Job, int(form.get("job_id") or 0))
    except ValueError:
        return redirect(f"/contacts/{contact_id}", err="Choose a job")
    if form.get("unlink") == "1":
        cs.unlink_contact_job(db, c, job)
        return redirect(form.get("next") or f"/contacts/{contact_id}", msg="Association removed")
    cs.link_contact_job(db, c, job, form.get("relation") or None, source="linked by you")
    return redirect(form.get("next") or f"/contacts/{contact_id}", msg=f"Linked to {job.title}")


@router.get("/companies")
def companies_page(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import func

    from jobpilot.models import Application

    rows = []
    for co in db.scalars(select(Company).order_by(Company.name)):
        rows.append({"c": co,
                     "jobs": db.scalar(select(func.count(Job.id)).where(Job.company_id == co.id)) or 0,
                     "active": db.scalar(select(func.count(Job.id)).where(Job.company_id == co.id, Job.status == "active")) or 0,
                     "contacts": db.scalar(select(func.count(Contact.id)).where(Contact.company_id == co.id)) or 0,
                     "applications": db.scalar(select(func.count(Application.id)).join(Job, Application.job_id == Job.id)
                                               .where(Job.company_id == co.id)) or 0})
    return render(request, "companies.html", rows=rows)


@router.get("/companies/{company_id:int}")
def company_detail(company_id: int, request: Request, db: Session = Depends(get_db)):
    from jobpilot.models import Application

    co = get_or_404(db, Company, company_id)
    jobs = list(db.scalars(select(Job).where(Job.company_id == co.id).order_by(Job.first_seen_at.desc())))
    contacts = list(db.scalars(select(Contact).where(Contact.company_id == co.id).order_by(Contact.name)))
    applications = list(db.scalars(select(Application).join(Job, Application.job_id == Job.id).where(Job.company_id == co.id)))
    blocked = cs.is_blocked(db, None, co.id)
    return render(request, "company_detail.html", co=co, jobs=jobs, contacts=contacts, applications=applications,
                  blocked=blocked)


@router.post("/companies/{company_id:int}")
async def company_save(company_id: int, request: Request, db: Session = Depends(get_db)):
    from jobpilot.services.text_utils import safe_url

    co = get_or_404(db, Company, company_id)
    form = await form_dict(request)
    for f in ("website", "careers_url"):
        v = form.get(f)
        if v and not safe_url(v):
            return redirect(f"/companies/{company_id}", err=f"{f.replace('_', ' ')} must be an http(s) URL")
        setattr(co, f, safe_url(v) if v else None)
    co.notes = (form.get("notes") or "")[:5000] or None
    audit.record(db, "company.updated", "company", co.id)
    return redirect(f"/companies/{company_id}", msg="Company saved")


@router.post("/jobs/{job_id:int}/contacts-from-posting")
def contacts_from_posting(job_id: int, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    created = cs.contacts_from_job(db, job)
    return redirect(f"/jobs/{job_id}", msg=f"{len(created)} contact(s) recorded from the posting (unverified)")


@router.post("/dnc")
async def dnc_add(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        cs.add_dnc(db, cs.DncIn.model_validate(form))
    except ValidationError as exc:
        return redirect("/contacts#dnc", err=first_error(exc))
    return redirect("/contacts#dnc", msg="Added to do-not-contact list")


@router.post("/dnc/{dnc_id:int}/delete")
def dnc_delete(dnc_id: int, db: Session = Depends(get_db)):
    cs.remove_dnc(db, dnc_id)
    return redirect("/contacts#dnc", msg="Removed from do-not-contact list")


# ---- Drafts --------------------------------------------------------------------

def _templates(db: Session) -> list[EmailTemplate]:
    return list(db.scalars(select(EmailTemplate).where(EmailTemplate.active.is_(True)).order_by(EmailTemplate.id)))


@router.get("/drafts")
def drafts_page(request: Request, db: Session = Depends(get_db)):
    status = request.query_params.get("status") or None
    stmt = select(EmailDraft).order_by(EmailDraft.updated_at.desc()).limit(300)
    if status:
        stmt = stmt.where(EmailDraft.status == status)
    due = ds.due_followups(db)  # read-only; suppression is applied by the scheduled follow-up check
    return render(request, "drafts.html", drafts=list(db.scalars(stmt)), due=due, status=status or "",
                  templates=_templates(db), kind_labels=ds.KIND_LABELS,
                  contacts=cs.search(db, include_dnc=False),
                  jobs=list(db.scalars(select(Job).where(Job.status == "active").order_by(Job.relevance_score.desc().nulls_last()).limit(200))))


@router.post("/drafts")
async def draft_create(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        d = ds.create_draft(db, ds.DraftCreate.model_validate(form))
    except (ValidationError, ds.DraftError) as exc:
        return redirect(form.get("back") or "/drafts", err=first_error(exc))
    return redirect(f"/drafts/{d.id}", msg="Draft created — review and edit before approving")


@router.get("/drafts/{draft_id:int}")
def draft_detail(draft_id: int, request: Request, db: Session = Depends(get_db)):
    d = get_or_404(db, EmailDraft, draft_id)
    from jobpilot.integrations import gmail

    return render(request, "draft_detail.html", d=d, revisions=ds.revisions(db, d), problems=ds.approval_problems(db, d) if d.status == "draft" else [],
                  warnings=ds.recipient_warnings(d), thread=ds.thread(db, d), kind_labels=ds.KIND_LABELS,
                  gmail_ready=gmail.is_ready(request.app.state.settings))


@router.post("/drafts/{draft_id:int}")
async def draft_edit(draft_id: int, request: Request, db: Session = Depends(get_db)):
    d = get_or_404(db, EmailDraft, draft_id)
    form = await form_dict(request)
    had_gmail_draft = bool(d.gmail_draft_id)
    try:
        ds.edit_draft(db, d, ds.DraftEdit.model_validate(form))
    except (ValidationError, ds.DraftError) as exc:
        return redirect(f"/drafts/{draft_id}", err=first_error(exc))
    if had_gmail_draft and not d.gmail_draft_id:
        return redirect(f"/drafts/{draft_id}", msg="Saved. The copy already in your Gmail Drafts is now outdated — delete "
                                                   "it there; JobPilot will create a new one from the approved text")
    return redirect(f"/drafts/{draft_id}", msg="Saved")


@router.post("/drafts/{draft_id:int}/{action}")
async def draft_action(draft_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    d = get_or_404(db, EmailDraft, draft_id)
    form = await form_dict(request)
    try:
        if action == "approve":
            ds.approve(db, d, acknowledge_guessed=form.get("ack_guessed") == "1")
            msg = "Approved. Send it yourself and click 'Mark as sent', or use Gmail if configured."
        elif action == "sent":
            ds.mark_sent(db, d, via="manual")
            msg = "Recorded as sent"
        elif action == "reply":
            ds.record_reply(db, d, form.get("summary"))
            msg = "Reply recorded; follow-ups on this thread suppressed"
        elif action == "cancel":
            ds.cancel(db, d)
            msg = "Draft cancelled"
        elif action == "followup":
            fu = ds.create_followup(db, d)
            return redirect(f"/drafts/{fu.id}", msg="Follow-up drafted — review before approving")
        elif action == "unapprove":
            if d.status == "approved":
                d.status, d.approved_at = "draft", None
            msg = "Moved back to draft"
        else:
            return redirect(f"/drafts/{draft_id}", err="Unknown action")
    except ds.DraftError as exc:
        return redirect(f"/drafts/{draft_id}", err=str(exc))
    return redirect(f"/drafts/{draft_id}", msg=msg)


@router.get("/drafts/templates")
def templates_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "templates.html", templates=_templates(db), kind_labels=ds.KIND_LABELS)


@router.post("/drafts/templates/{template_id:int}")
async def template_save(template_id: int, request: Request, db: Session = Depends(get_db)):
    t = get_or_404(db, EmailTemplate, template_id)
    form = await form_dict(request)
    if form.get("reset") and t.key in ds.BUILTIN_TEMPLATES:
        t.subject, t.body = ds.BUILTIN_TEMPLATES[t.key]["subject"], ds.BUILTIN_TEMPLATES[t.key]["body"]
        return redirect("/drafts/templates", msg="Template reset")
    subject, body = form.get("subject") or "", form.get("body") or ""
    try:
        ds.render(subject, {})
        ds.render(body, {})
    except Exception as exc:  # template syntax errors
        return redirect("/drafts/templates", err=f"Template error: {exc}")
    if not subject or not body:
        return redirect("/drafts/templates", err="Subject and body are required")
    t.subject, t.body = subject, body
    audit.record(db, "template.updated", "email_template", t.id)
    return redirect("/drafts/templates", msg="Template saved")
