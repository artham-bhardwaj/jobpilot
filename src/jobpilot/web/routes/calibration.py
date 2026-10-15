"""Matching calibration page: score components, your feedback, weight preview and explicit save."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from jobpilot.models import Job
from jobpilot.services import calibration, settings_store
from jobpilot.services.settings_store import WEIGHT_KEYS
from jobpilot.web.deps import first_error, form_dict, get_db, get_or_404, redirect, render

router = APIRouter()


def _weights_from(params, base: dict) -> tuple[dict, bool]:
    proposed, changed = dict(base), False
    for k in WEIGHT_KEYS:
        raw = params.get(f"w_{k}")
        if raw not in (None, ""):
            try:
                v = float(raw)
            except ValueError:
                continue
            if 0 <= v <= 100 and v != float(base[k]):
                proposed[k], changed = v, True
    return proposed, changed


@router.get("/matching")
def calibration_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    mode = qp.get("mode") if qp.get("mode") in calibration.SAMPLE_MODES else "recent"
    try:
        n = max(5, min(100, int(qp.get("n") or 25)))
        seed = int(qp.get("seed") or 0)
    except ValueError:
        n, seed = 25, 0
    current = settings_store.get(db, "matching_weights")
    proposed, changed = _weights_from(qp, current)
    jobs = calibration.sample(db, mode, n, seed)
    previews = calibration.preview(db, jobs, proposed) if changed else {}
    total = sum(float(v) for v in current.values()) or 1.0
    return render(request, "calibration.html", jobs=jobs, mode=mode, n=n, seed=seed, modes=calibration.SAMPLE_MODES,
                  weights=current, weights_pct={k: round(float(v) * 100 / total, 1) for k, v in current.items()},
                  proposed=proposed, changed=changed, previews=previews, keys=WEIGHT_KEYS,
                  feedback=calibration.latest_feedback(db), report=calibration.report(db),
                  matching=settings_store.get(db, "matching"))


@router.post("/matching/feedback/{job_id:int}")
async def calibration_feedback(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = get_or_404(db, Job, job_id)
    form = await form_dict(request)
    try:
        calibration.record_feedback(db, job, form.get("verdict") or "", form.get("note"))
    except ValueError as exc:
        return redirect(form.get("next") or "/matching", err=str(exc))
    return redirect(form.get("next") or "/matching", msg=f"Marked as {form.get('verdict')} (scoring unchanged)")


@router.post("/matching/weights")
async def calibration_weights(request: Request, db: Session = Depends(get_db)):
    form = await form_dict(request)
    try:
        weights = {k: float(form.get(f"w_{k}") or 0) for k in WEIGHT_KEYS}
        calibration.save_weights(db, weights)
    except (ValidationError, ValueError) as exc:
        return redirect("/matching", err=f"Weights not saved: {first_error(exc)}")
    return redirect("/matching", msg="Weights saved. Existing scores are unchanged until you click 'Re-score all jobs'.")
