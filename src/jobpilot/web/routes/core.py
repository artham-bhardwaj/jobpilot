"""Dashboard overview, health, system (logs, tasks, audit, backups) and settings."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.logging_setup import tail_log
from jobpilot.models import TaskRun
from jobpilot.services import audit, backup, health, overview, privacy, scheduler, settings_store
from jobpilot.services.tasks import run_task
from jobpilot.web.deps import form_dict, get_db, get_settings_dep, redirect, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    return render(request, "dashboard.html", stats=overview.stats(db))


@router.get("/health")
def health_json(db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    result = health.check(settings, db)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@router.get("/system")
def system_page(request: Request, level: str | None = None, db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings_dep)):
    runs = list(db.scalars(select(TaskRun).order_by(TaskRun.id.desc()).limit(50)))
    return render(
        request, "system.html",
        health=health.check(settings, db),
        logs=tail_log(settings, limit=150, level=level),
        level=level,
        runs=runs,
        audit_entries=audit.recent(db, limit=100),
        backups=backup.list_backups(settings),
        sched_tasks=[(n, scheduler.last_success(db, n)) for n in scheduler.TASKS],
        sched_due=scheduler.due_tasks(db),
        sched_next=scheduler.next_runs(db), sched_labels=scheduler.TASK_LABELS,
        sched_cfg=settings_store.get(db, "scheduler"),
    )


@router.post("/system/tasks/{name}")
def system_run_task(name: str, request: Request):
    if name not in scheduler.TASKS:
        return redirect("/system", err="Unknown task")
    settings, factory = request.app.state.settings, request.app.state.sessionmaker
    run = run_task(settings, factory, name, scheduler.TASKS[name](settings), trigger="manual", retries=1)
    return redirect("/system#tasks", **({"msg": f"{name}: {run.message or run.status}"} if run.status == "success"
                                        else {"err": f"{name} {run.status}: {run.message}"}))


@router.get("/system/export.json")
def system_export(db: Session = Depends(get_db)):
    data = json.dumps(privacy.export_all(db), indent=1, default=str)
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="jobpilot-export.json"'})


@router.post("/system/delete")
async def system_delete(request: Request):
    form = await form_dict(request)
    scope = form.get("scope") or ""
    if form.get("confirm") != "DELETE":
        return redirect("/system#privacy", err="Type DELETE to confirm")
    settings, engine = request.app.state.settings, request.app.state.engine
    include_backups = form.get("include_backups") == "1"
    if not include_backups:
        backup.create_backup(settings, engine, label=f"pre-delete-{scope}")
    try:
        with request.app.state.sessionmaker() as s:
            privacy.delete_data(s, settings, scope, include_backups=include_backups)
            s.commit()
    except ValueError as exc:
        return redirect("/system#privacy", err=str(exc))
    return redirect("/system#privacy", msg=f"Deleted {scope} data" + ("" if include_backups else " (a backup was taken first)"))


@router.post("/system/backup")
def system_backup(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    path = backup.create_backup(settings, request.app.state.engine, label="manual")
    audit.record(db, "backup.created", file=path.name)
    return redirect("/system", msg=f"Backup created: {path.name}")


@router.get("/system/backup/{name}")
def system_backup_download(name: str, request: Request):
    try:
        path = backup.resolve_backup(request.app.state.settings, name)
    except backup.BackupNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except backup.BackupError as exc:
        raise HTTPException(status_code=400, detail=f"{exc}: expected a name like jobpilot-<timestamp>-<label>.zip") from exc
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.post("/system/restore")
async def system_restore(request: Request):
    form = await form_dict(request)
    settings = request.app.state.settings
    if form.get("confirm") != "RESTORE":
        return redirect("/system", err="Type RESTORE to confirm restoring a backup.")
    try:
        path = backup.resolve_backup(settings, form.get("name") or "")
        result = backup.restore_backup(settings, path, request.app.state.engine)
    except backup.BackupError as exc:
        return redirect("/system", err=str(exc))
    with request.app.state.sessionmaker() as s:
        audit.record(s, "backup.restored", file=path.name, pre_restore=result["pre_restore_backup"])
        s.commit()
    return redirect("/system", msg=f"Restored {path.name}. Previous state saved first.")


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    keys = list(settings_store.DEFAULTS)
    values = {k: json.dumps(settings_store.get(db, k), indent=2) for k in keys}
    return render(request, "settings.html", values=values)


@router.post("/settings/{key}")
async def settings_save(key: str, request: Request, db: Session = Depends(get_db)):
    if key not in settings_store.DEFAULTS:
        return redirect("/settings", err="Unknown setting")
    form = await form_dict(request)
    if form.get("reset"):
        settings_store.reset(db, key)
        return redirect("/settings", msg=f"{key} reset to defaults")
    try:
        value = json.loads(form.get("value") or "null")
        settings_store.validate(key, value)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return redirect("/settings", err=f"{key}: invalid value ({exc})")
    settings_store.put(db, key, value)
    return redirect("/settings", msg=f"{key} saved")
