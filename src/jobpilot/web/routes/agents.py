"""The Agents page: what the Search, Resume, Apply and Manager agents did, what needs you, and their controls."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Application, ReviewItem
from jobpilot.services import review, settings_store
from jobpilot.services.tasks import run_task
from jobpilot.web.deps import form_dict, get_db, redirect, render

router = APIRouter()

AGENT_INFO = [
    ("manager", "Manager", "Picks the jobs, runs the other three in order, keeps the daily limit, reports to you."),
    ("search", "Search", "Every source, public feeds, job-alert emails, and new company boards it finds."),
    ("resume", "Resume", "A validated resume tailored to each picked job. Nothing invented."),
    ("apply", "Apply", "Emails or submits where it's safe; hands you everything else, ready to go."),
]


@router.get("/agents")
def agents_page(request: Request, db: Session = Depends(get_db)):
    from jobpilot.agents.apply import NEEDS_YOU
    from jobpilot.agents.base import applied_today, is_paused, last_report, policy
    from jobpilot.services import scheduler

    items = list(db.scalars(select(ReviewItem).where(ReviewItem.kind == NEEDS_YOU, ReviewItem.status == "open")
                            .order_by(ReviewItem.id.desc())))
    apps = {a.id: a for a in db.scalars(select(Application).where(Application.id.in_([i.entity_id for i in items])))}
    sched = settings_store.get(db, "scheduler")["tasks"].get("agents", {})
    cmds = {i.id: m.group(1) for i in items if (m := re.search(r"`(jobpilot assist [^`]+)`", i.reason or ""))}
    return render(request, "agents.html", pol=policy(db), paused=is_paused(db), today=applied_today(db),
                  reports={k: last_report(db, k) for k, *_ in AGENT_INFO}, info=AGENT_INFO, items=items, apps=apps,
                  sched=sched, cmds=cmds, next_run=scheduler.next_runs(db).get("agents"),
                  host_of=lambda u: urlsplit(u or "").hostname or "")


@router.post("/agents/run")
async def agents_run(request: Request, background: BackgroundTasks):
    from jobpilot.agents import run_cycle

    form = await form_dict(request)
    only = form.get("only") if form.get("only") in ("search", "resume", "apply") else None
    settings, factory = request.app.state.settings, request.app.state.sessionmaker

    def _job():
        run_task(settings, factory, "agents", lambda s: run_cycle(s, settings, only=only), trigger="manual")

    background.add_task(_job)
    return redirect("/agents", msg="Cycle started" + (f" ({only} agent)" if only else "")
                    + ". It runs in the background; refresh in a minute or two.")


@router.post("/agents/mode")
async def agents_mode(request: Request, db: Session = Depends(get_db)):
    from jobpilot.agents.base import policy

    form = await form_dict(request)
    mode = form.get("mode")
    if mode not in ("dry_run", "auto"):
        return redirect("/agents", err="Unknown mode")
    if mode == "auto" and form.get("confirm") != "1":
        return redirect("/agents", err="Tick the confirmation to let the agents send and submit applications")
    pol = {**policy(db), "apply_mode": mode}
    settings_store.validate("agents", pol)
    settings_store.put(db, "agents", pol)
    return redirect("/agents", msg="Applying automatically (within the daily limit)" if mode == "auto"
                    else "Dry run: nothing is sent or submitted")


@router.post("/agents/policy")
async def agents_policy(request: Request, db: Session = Depends(get_db)):
    from jobpilot.agents.base import policy

    form = await form_dict(request)
    pol = dict(policy(db))
    try:
        pol["min_score"] = float(form.get("min_score") or pol["min_score"])
        pol["daily_limit"] = int(form.get("daily_limit") or pol["daily_limit"])
        pol["max_per_cycle"] = int(form.get("max_per_cycle") or pol["max_per_cycle"])
        elig = form.get("allowed_eligibility")
        if elig:
            pol["allowed_eligibility"] = elig if isinstance(elig, list) else [elig]
        for flag in ("email_applications", "form_submissions", "hold_dream_companies", "job_alert_emails",
                     "discover_boards"):
            pol[flag] = form.get(flag) == "1"
        settings_store.validate("agents", pol)
    except (ValueError, TypeError) as exc:
        return redirect("/agents", err=f"Not saved: {str(exc)[:200]}")
    settings_store.put(db, "agents", pol)
    return redirect("/agents", msg="Policy saved")


@router.post("/agents/pause")
async def agents_pause(request: Request, db: Session = Depends(get_db)):
    from jobpilot.agents.base import set_paused

    form = await form_dict(request)
    paused = form.get("action") == "pause"
    set_paused(db, paused, "dashboard")
    return redirect("/agents", msg="Paused: nothing is sent or submitted" if paused else "Resumed")


@router.post("/agents/items/{item_id:int}")
async def agents_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    review.resolve(db, item_id, "dismissed" if form.get("action") == "dismiss" else "resolved",
                   form.get("note") or "handled on the Agents page")
    return redirect("/agents", msg="Done")


@router.post("/agents/schedule")
async def agents_schedule(request: Request, db: Session = Depends(get_db)):
    """Turn the Manager's scheduled cycle on or off (Settings → scheduler → agents)."""
    form = await form_dict(request)
    cfg = settings_store.get(db, "scheduler")
    task = dict(cfg["tasks"].get("agents") or {"every_hours": 3})
    task["enabled"] = form.get("enabled") == "1"
    try:
        hours = float(form.get("every_hours") or task.get("every_hours") or 3)
    except ValueError:
        hours = 3
    task["every_hours"] = min(max(hours, 1), 24)
    cfg["tasks"]["agents"] = task
    settings_store.validate("scheduler", cfg)
    settings_store.put(db, "scheduler", cfg)
    return redirect("/agents", msg=f"Agents run every {task['every_hours']:g} h" if task["enabled"]
                    else "Scheduled cycles off (Run a cycle now still works)")
