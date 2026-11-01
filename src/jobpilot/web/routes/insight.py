"""Interviews, skill gaps, analytics/CSV export and the optional Gmail integration."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.integrations import gmail
from jobpilot.models import Application, EmailDraft, Interview, LearningItem, ReviewItem
from jobpilot.models.interviews import INTERVIEW_FORMATS, INTERVIEW_STAGES, LEARNING_STATUSES
from jobpilot.services import analytics, interviews as iv_svc, review, settings_store, skill_gaps
from jobpilot.services.tasks import run_task
from jobpilot.web.deps import first_error, form_dict, get_db, get_or_404, redirect, render

router = APIRouter()


# ---- Interviews -------------------------------------------------------------------

def _active_apps(db: Session) -> list[Application]:
    return list(db.scalars(select(Application).where(Application.status.in_(("applied", "assessment", "interview", "offer")))
                           .order_by(Application.updated_at.desc())))


@router.get("/interviews")
def interviews_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    prefill = {}
    if qp.get("suggestion", "").isdigit():
        item = db.get(ReviewItem, int(qp["suggestion"]))
        if item and item.kind == "interview_detected":
            prefill = dict(item.data or {}, application_id=item.entity_id, suggestion_id=item.id)
    elif qp.get("application_id", "").isdigit():
        prefill = {"application_id": int(qp["application_id"])}
    return render(request, "interviews.html", upcoming=iv_svc.upcoming(db), past=iv_svc.past(db), apps=_active_apps(db),
                  stages=INTERVIEW_STAGES, formats=INTERVIEW_FORMATS, prefill=prefill)


@router.post("/interviews")
@router.post("/interviews/{iv_id:int}")
async def interview_save(request: Request, iv_id: int | None = None, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        iv = iv_svc.save(db, iv_svc.InterviewIn.model_validate(form), iv_id,
                         source="email" if form.get("suggestion_id") else "manual")
    except (ValidationError, ValueError, LookupError) as exc:
        return redirect("/interviews", err=first_error(exc))
    if form.get("suggestion_id", "").isdigit():
        review.resolve(db, int(form["suggestion_id"]), "resolved", f"confirmed as interview #{iv.id}")
    return redirect(f"/interviews/{iv.id}", msg="Interview saved")


@router.get("/interviews/{iv_id:int}")
def interview_detail(iv_id: int, request: Request, db: Session = Depends(get_db)):
    iv = get_or_404(db, Interview, iv_id)
    drafts = list(db.scalars(select(EmailDraft).where(EmailDraft.job_id == iv.application.job_id, EmailDraft.kind == "thank_you")))
    return render(request, "interview_detail.html", iv=iv, stages=INTERVIEW_STAGES, formats=INTERVIEW_FORMATS,
                  pack=iv.prep_packs[0] if iv.prep_packs else None, thank_you=drafts)


@router.post("/interviews/{iv_id:int}/outcome")
async def interview_outcome(iv_id: int, request: Request, db: Session = Depends(get_db)):
    iv = get_or_404(db, Interview, iv_id)
    form = await form_dict(request)
    form["follow_up_done"] = form.get("follow_up_done") == "1"
    try:
        iv_svc.record_outcome(db, iv, iv_svc.OutcomeIn.model_validate(form))
    except ValidationError as exc:
        return redirect(f"/interviews/{iv_id}", err=first_error(exc))
    return redirect(f"/interviews/{iv_id}", msg="Updated")


@router.post("/interviews/{iv_id:int}/prep")
def interview_prep(iv_id: int, db: Session = Depends(get_db)):
    iv = get_or_404(db, Interview, iv_id)
    iv_svc.generate_prep_pack(db, iv)
    return redirect(f"/interviews/{iv_id}#prep", msg="Preparation pack generated")


@router.post("/applications/{app_id:int}/invitation")
async def paste_invitation(app_id: int, request: Request, db: Session = Depends(get_db)):
    app = get_or_404(db, Application, app_id)
    form = await form_dict(request)
    s = iv_svc.suggest_from_text(db, app, form.get("text") or "", source_ref="pasted")
    if s is None:
        return redirect("/interviews", err="Couldn't find interview details (date or meeting link) in that text")
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "interview_detected", ReviewItem.entity_id == app.id,
                                              ReviewItem.status == "open"))
    return redirect(f"/interviews?suggestion={item.id}", msg="Details extracted — check and confirm them below")


# ---- Skill gaps ---------------------------------------------------------------------

@router.get("/skills")
def skills_page(request: Request, db: Session = Depends(get_db)):
    scope = request.query_params.get("scope") or "relevant"
    category = request.query_params.get("category") or None
    result = skill_gaps.analyze(db, scope=scope, category=category)
    backlog = list(db.scalars(select(LearningItem).order_by(LearningItem.status, LearningItem.priority.desc().nulls_last())))
    return render(request, "skills.html", r=result, backlog=backlog, scope=scope, category=category or "",
                  categories=settings_store.get(db, "role_categories"), statuses=LEARNING_STATUSES)


@router.post("/skills/backlog")
async def skills_backlog_add(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    name = (form.get("skill") or "").strip().lower()
    if not name:
        return redirect("/skills", err="Skill required")
    try:
        prio = float(form["priority"]) if form.get("priority") else None
    except ValueError:
        prio = None
    skill_gaps.add_to_backlog(db, name, prio)
    return redirect("/skills#backlog", msg=f"'{name}' added to your learning backlog (marked as learning)")


@router.post("/skills/backlog/{item_id:int}")
async def skills_backlog_update(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = get_or_404(db, LearningItem, item_id)
    form = await form_dict(request)
    try:
        if form.get("action") == "confirm":
            skill_gaps.confirm_mastery(db, item, form.get("evidence") or "")
            return redirect("/skills#backlog", msg=f"'{item.skill_name}' confirmed as a skill")
        skill_gaps.update(db, item, skill_gaps.LearningUpdate.model_validate(form))
    except (ValidationError, ValueError) as exc:
        return redirect("/skills#backlog", err=first_error(exc))
    return redirect("/skills#backlog", msg="Updated")


# ---- Analytics ------------------------------------------------------------------------

@router.get("/analytics")
def analytics_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "analytics.html", s=analytics.summary(db), exports=analytics.EXPORT_KINDS)


@router.get("/analytics/export/{kind}.csv")
def analytics_export(kind: str, db: Session = Depends(get_db)):
    if kind not in analytics.EXPORT_KINDS:
        return redirect("/analytics", err="Unknown export")
    data = analytics.export_csv(db, kind)
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="jobpilot-{kind}-{stamp}.csv"'})


# ---- Gmail ---------------------------------------------------------------------------

@router.get("/gmail")
def gmail_page(request: Request):
    settings = request.app.state.settings
    return render(request, "gmail.html", st=gmail.status(settings), ready=gmail.readiness(settings))


def _client(request: Request) -> gmail.GmailClient:
    return gmail.GmailClient(request.app.state.settings)


@router.get("/gmail/drafts/{draft_id:int}/send")
def gmail_send_confirm(draft_id: int, request: Request, db: Session = Depends(get_db)):
    """Confirmation screen: exactly what would be sent. Builds the message locally; contacts nothing."""
    d = get_or_404(db, EmailDraft, draft_id)
    settings = request.app.state.settings
    preview = gmail.send_preview(db, settings, d)
    ready = gmail.readiness(settings)
    return render(request, "gmail_send.html", d=d, preview=preview, ready=ready)


@router.post("/gmail/drafts/{draft_id:int}/{action}")
async def gmail_draft_action(draft_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    d = get_or_404(db, EmailDraft, draft_id)
    form = await form_dict(request)
    settings = request.app.state.settings
    ready = gmail.readiness(settings)
    if not ready["ready"]:
        return redirect(f"/drafts/{draft_id}", err="Gmail unavailable: " + "; ".join(ready["problems"]))
    try:
        if action == "create":
            res = gmail.create_gmail_draft(db, settings, d, _client(request))
            att = res.get("attachment")
            what = f" with {att['filename']} attached ({att['size']:,} bytes)" if att else " (no attachment)"
            msg = (f"Gmail confirmed: the draft is in your Gmail Drafts{what}. Gmail draft id {res['id']}."
                   if not res["reused"] else f"Already in your Gmail Drafts (id {res['id']}); no duplicate was created.")
            msg += " Nothing was sent."
        elif action == "send":
            gmail.send_via_gmail(db, settings, d, _client(request), form.get("confirm") or "",
                                 expected_hash=form.get("content_hash"))
            msg = f"Gmail confirmed the message was sent (message id {d.gmail_message_id})."
        elif action == "check":
            msg = "Gmail check: " + gmail.reconcile(db, settings, d, _client(request))["message"]
        else:
            return redirect(f"/drafts/{draft_id}", err="Unknown action")
    except gmail.GmailError as exc:
        return redirect(f"/drafts/{draft_id}", err=str(exc))
    return redirect(f"/drafts/{draft_id}", msg=msg)


@router.get("/telegram")
def telegram_page(request: Request, db: Session = Depends(get_db)):
    from jobpilot.integrations import telegram
    from jobpilot.services import notifications

    settings = request.app.state.settings
    return render(request, "telegram.html", st=telegram.config_status(settings), recent=notifications.recent(db, 25),
                  counts=notifications.counts(db), baseline=notifications.get_state(db, notifications.BASELINE_KEY),
                  poller=getattr(request.app.state, "telegram_thread", None) is not None)


@router.post("/telegram/{action}")
def telegram_action(action: str, request: Request):
    """Explicit actions only: each sends at most the messages it says, to your configured chat."""
    from jobpilot.integrations import telegram
    from jobpilot.services import notifications

    settings, factory = request.app.state.settings, request.app.state.sessionmaker
    st = telegram.config_status(settings)
    if not st["ready"]:
        return redirect("/telegram", err="Telegram not configured: " + "; ".join(st["problems"]))
    if action == "test":
        try:
            ids = telegram.TelegramClient(settings).send_text("✅ JobPilot test message from the dashboard. No action needed.")
        except telegram.TelegramError as exc:
            return redirect("/telegram", err=f"Not delivered: {telegram.redact(str(exc), settings)}")
        return redirect("/telegram", msg=f"Telegram confirmed delivery (message id {ids[0]})")
    if action == "notify":
        run = run_task(settings, factory, "notify", lambda s: notifications.run(s, settings))
        if run.status == "success":
            return redirect("/telegram", msg=run.message or "Done")
        return redirect("/telegram", err=f"{run.status}: {run.message}")
    return redirect("/telegram", err="Unknown action")


@router.post("/gmail/{action}")
def gmail_action(action: str, request: Request):
    settings, factory = request.app.state.settings, request.app.state.sessionmaker
    if action == "disconnect":
        gmail.revoke(settings)
        return redirect("/gmail", msg="Disconnected and token deleted")
    ready = gmail.readiness(settings)
    if not ready["ready"]:
        return redirect("/gmail", err="Gmail unavailable: " + "; ".join(ready["problems"]))
    if action == "sync":
        run = run_task(settings, factory, "gmail_sync", lambda s: gmail.sync_replies(s, settings, gmail.GmailClient(settings)))
    elif action == "interviews":
        run = run_task(settings, factory, "gmail_interviews",
                       lambda s: gmail.import_interview_emails(s, settings, gmail.GmailClient(settings)))
    else:
        return redirect("/gmail", err="Unknown action")
    if run.status == "success":
        return redirect("/gmail", msg=run.message or "Done")
    return redirect("/gmail", err=f"{run.status}: {run.message}")
