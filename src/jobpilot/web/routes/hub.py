"""Personal information hub: one view over the profile, with provenance, imports and conflicts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from jobpilot.models import ProfileClaim, ProfileImport, ResumeVersion
from jobpilot.models.hub import CLAIM_STATES
from jobpilot.services import hub
from jobpilot.web.deps import form_dict, get_db, get_or_404, get_settings_dep, redirect, render

router = APIRouter()

CATEGORIES = ["profile", "contact", "link", "employment", "education", "skill", "project", "certification",
              "achievement", "answer", "resume"]


@router.get("/hub")
def hub_page(request: Request, q: str | None = None, state: str | None = None, category: str | None = None,
             db: Session = Depends(get_db)):
    ov = hub.overview(db)
    results = hub.search_claims(db, q, state, category) if (q or state or category) else None
    return render(request, "hub.html", ov=ov, results=results, q=q or "", state=state or "", category=category or "",
                  states=list(CLAIM_STATES) + ["open"], categories=CATEGORIES)


@router.post("/hub/import")
async def hub_import(request: Request, db: Session = Depends(get_db), settings=Depends(get_settings_dep)):
    form = await form_dict(request)
    dry = form.get("dry_run") == "1"
    upload = form.get("file")
    try:
        if upload is not None and getattr(upload, "filename", None):
            data = await upload.read(settings.max_upload_bytes + 1)
            await upload.close()
            if len(data) > settings.max_upload_bytes:
                raise ValueError(f"File too large (max {settings.max_upload_bytes // 1_000_000} MB)")
            imp = hub.import_resume(db, settings, upload.filename, data, dry_run=dry)
        else:
            version = get_or_404(db, ResumeVersion, int(form.get("version_id") or 0), "resume version")
            imp = hub.import_registered_version(db, settings, version, dry_run=dry)
    except (hub.HubImportError, ValueError) as exc:
        return redirect("/hub#imports", err=f"Import failed: {exc}")
    c = imp.counts or {}
    msg = (f"{'Dry run' if dry else 'Imported'}: {c.get('created', 0)} created, {c.get('updated', 0)} updated, "
           f"{c.get('unchanged', 0)} unchanged, {c.get('conflict', 0)} conflicts, {c.get('suggestion', 0)} suggestions")
    return redirect(f"/hub/imports/{imp.id}", msg=msg)


@router.get("/hub/imports/{import_id:int}")
def import_detail(import_id: int, request: Request, db: Session = Depends(get_db)):
    imp = get_or_404(db, ProfileImport, import_id, "import")
    return render(request, "hub_import.html", imp=imp)


@router.post("/hub/claims/{claim_id:int}/{action}")
def claim_action(claim_id: int, action: str, db: Session = Depends(get_db)):
    claim = get_or_404(db, ProfileClaim, claim_id, "claim")
    try:
        if action == "apply":
            hub.apply_claim(db, claim)
            msg = "Resume value applied to your profile"
        elif action == "dismiss":
            hub.dismiss_claim(db, claim)
            msg = "Kept your profile's value; the resume value is marked rejected"
        else:
            return redirect("/hub#attention", err=f"Unknown action {action[:20]}")
    except (ValueError, LookupError) as exc:
        return redirect("/hub#attention", err=str(exc))
    return redirect("/hub#attention", msg=msg)


@router.get("/api/hub")
def api_hub(db: Session = Depends(get_db)):
    return JSONResponse(hub.snapshot(db))


@router.get("/api/hub/claims")
def api_claims(q: str | None = None, state: str | None = None, category: str | None = None,
               db: Session = Depends(get_db)):
    rows = hub.search_claims(db, q, state, category)
    return JSONResponse([{"id": r["claim"].id, "category": r["claim"].category, "field": r["claim"].field,
                          "value": r["claim"].value, "original": r["claim"].original_text, "state": r["state"],
                          "outcome": r["claim"].outcome, "source": r["claim"].source_label,
                          "location": r["claim"].source_location, "entity": r["claim"].entity_type,
                          "entity_id": r["claim"].entity_id} for r in rows])


@router.get("/api/hub/imports/{import_id:int}")
def api_import(import_id: int, db: Session = Depends(get_db)):
    imp = get_or_404(db, ProfileImport, import_id, "import")
    return JSONResponse({"id": imp.id, "status": imp.status, "file": imp.source_filename, "sha256": imp.source_sha256,
                         "method": imp.extraction_method, "counts": imp.counts, "report": imp.report,
                         "error": imp.error, "resume_version_id": imp.resume_version_id,
                         "created_at": imp.created_at.isoformat()})
