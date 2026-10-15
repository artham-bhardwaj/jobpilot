"""Browser-assisted application filling (optional, Playwright).

What it does:
- Opens the application page in a visible Chrome window with a dedicated local profile.
- Autofills **only** on supported hosted forms (Greenhouse, Lever), and only fields that map
  to *confirmed* profile facts (name, email, phone, location, links) plus the selected
  approved resume file.
- Pauses — fills nothing further — on CAPTCHA, login, MFA/verification-code prompts or
  security checks, and lists unknown questions, consent boxes and legal/EEO declarations
  for you to answer yourself.
- **Never clicks submit** and never ticks checkboxes. You review and submit in the window.
- If a confirmation page appears, its text and a screenshot are saved as *candidate*
  evidence; you still confirm the submission in JobPilot.

It never bypasses access controls or anti-bot measures.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.config import Settings
from jobpilot.models import Application, BrowserRun, Employment, ProfileLink, utcnow
from jobpilot.services import audit, profile as profile_svc, review, storage

log = logging.getLogger(__name__)

SUPPORTED_HOSTS = {
    "greenhouse": re.compile(r"(^|\.)(boards|job-boards)(\.eu)?\.greenhouse\.io$"),
    "lever": re.compile(r"(^|\.)jobs(\.eu)?\.lever\.co$"),
}
BLOCKER_IFRAME = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha|captcha", re.I)
BLOCKER_TEXT = re.compile(r"verify (?:that )?you are (?:a )?human|are you a robot|security check|captcha|"
                          r"unusual traffic|access denied", re.I)
MFA_TEXT = re.compile(r"verification code|one[- ]time (?:pass)?code|\botp\b|two[- ]factor|2fa|authenticator", re.I)
CONFIRM_TEXT = re.compile(r"thank you for (?:applying|your application)|application (?:has been )?(?:received|submitted)|"
                          r"we(?:'ve| have) received your application|successfully submitted", re.I)
SENSITIVE = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|pronoun|religio|caste|"
                       r"date of birth|\bdob\b|\bage\b|marital|consent|agree|terms|privacy|policy|certify|declar|"
                       r"acknowledg|attest|authori[sz]ed to work|sponsor|visa|criminal|background check|salary|"
                       r"compensation|ctc|notice period|relocat", re.I)

FIELD_RULES: list[tuple[str, re.Pattern]] = [
    ("first_name", re.compile(r"first[\s_-]*name|given[\s_-]*name|^first$", re.I)),
    ("last_name", re.compile(r"last[\s_-]*name|surname|family[\s_-]*name|^last$", re.I)),
    ("full_name", re.compile(r"^(full[\s_-]*)?name$|full[\s_-]*name|your name", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|telephone", re.I)),
    ("linkedin", re.compile(r"linked[\s_-]*in", re.I)),
    ("github", re.compile(r"github", re.I)),
    ("website", re.compile(r"website|portfolio|personal (site|url)|urls\[other\]", re.I)),
    ("location", re.compile(r"^location$|current location|city|where are you (based|located)", re.I)),
    ("current_company", re.compile(r"current (company|employer)|^org$|organi[sz]ation", re.I)),
]
RESUME_FIELD = re.compile(r"resume|cv|curriculum", re.I)

COLLECT_FIELDS_JS = """
() => {
  const out = [];
  const els = Array.from(document.querySelectorAll('input, select, textarea'));
  els.forEach((el, i) => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    const style = window.getComputedStyle(el);
    const visible = type === 'file' || (style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null);
    if (!visible) return;
    let label = '';
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
    if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    if (!label) { const c = el.closest('.field, .application-question, .form-group, li, div'); if (c) label = (c.innerText || '').slice(0, 200); }
    const marker = 'jp-' + i;
    el.setAttribute('data-jobpilot', marker);
    out.push({marker, tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '',
              label: (label || '').trim().slice(0, 200), required: el.required || el.getAttribute('aria-required') === 'true'
              || /\\*\\s*$/.test((label || '').trim()), value: el.type === 'file' ? (el.files.length ? 'file' : '') : (el.value || ''),
              checked: !!el.checked});
  });
  return out;
}
"""


@dataclass
class AssistResult:
    status: str = "filled"  # filled | paused | unsupported | failed
    filled: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)
    pause_reasons: list[str] = field(default_factory=list)
    message: str = ""


def site_for(url: str | None) -> str | None:
    host = urlsplit(url or "").hostname or ""
    for name, rx in SUPPORTED_HOSTS.items():
        if rx.search(host):
            return name
    return None


def classify(f: dict) -> tuple[str, str | None]:
    """Decide what to do with a form field: ('fill', key) | ('upload', 'resume') | ('sensitive'|'unknown'|'skip', None)."""
    text = " ".join(x for x in (f.get("label"), f.get("name"), f.get("id")) if x)
    if f["type"] == "file":
        return ("upload", "resume") if RESUME_FIELD.search(text) else ("unknown", None)
    if SENSITIVE.search(text) or f["type"] in ("checkbox", "radio"):
        return "sensitive", None
    if f["type"] == "password":
        return "sensitive", None
    for key, rx in FIELD_RULES:
        if rx.search(f.get("label") or "") or rx.search(f.get("name") or "") or rx.search(f.get("id") or ""):
            return "fill", key
    return ("unknown", None) if f.get("required") else ("skip", None)


def profile_values(session: Session) -> dict[str, str]:
    """Values from *confirmed* facts only. Empty if the profile is not confirmed."""
    p = profile_svc.get_profile(session)
    if p.confirmed_at is None:
        return {}
    vals = {"first_name": p.first_name or "", "last_name": p.last_name or "",
            "full_name": p.full_name or " ".join(x for x in (p.first_name, p.last_name) if x),
            "email": p.email or "", "phone": p.phone or "", "location": p.current_location or ""}
    if not vals["first_name"] and vals["full_name"]:
        vals["first_name"] = vals["full_name"].split()[0]
    if not vals["last_name"] and len(vals["full_name"].split()) > 1:
        vals["last_name"] = vals["full_name"].split()[-1]
    for link in session.scalars(select(ProfileLink).where(ProfileLink.status == "confirmed")):
        label, url = link.label.lower(), link.url
        if "linkedin" in label or "linkedin.com" in url:
            vals.setdefault("linkedin", url)
        elif "github" in label or "github.com" in url:
            vals.setdefault("github", url)
        else:
            vals.setdefault("website", url)
    current = session.scalar(select(Employment).where(Employment.status == "confirmed", Employment.end_date.is_(None))
                             .order_by(Employment.start_date.desc()))
    if current:
        vals["current_company"] = current.company
    return {k: v for k, v in vals.items() if v}


def detect_blockers(page) -> list[str]:
    reasons = []
    for frame in page.frames:
        if frame.url and BLOCKER_IFRAME.search(frame.url):
            reasons.append("CAPTCHA / bot check present — complete it yourself")
            break
    try:
        body = page.inner_text("body", timeout=3000)[:20000]
    except Exception:
        body = ""
    if BLOCKER_TEXT.search(body) and not reasons:
        reasons.append("security check text detected on page")
    if page.query_selector("input[type=password]"):
        reasons.append("login required — sign in yourself")
    if MFA_TEXT.search(body) and page.query_selector("input[autocomplete='one-time-code'], input[name*='code' i], input[name*='otp' i]"):
        reasons.append("verification code / MFA prompt")
    return reasons


def assist_page(page, values: dict[str, str], resume_path: Path | None) -> AssistResult:
    """Fill a loaded application page. Never submits. Testable against any page."""
    res = AssistResult()
    res.pause_reasons = detect_blockers(page)
    if res.pause_reasons:
        res.status = "paused"
        res.message = "Paused before filling: " + "; ".join(res.pause_reasons)
        return res
    fields = page.evaluate(COLLECT_FIELDS_JS)
    for f in fields:
        action, key = classify(f)
        label = f.get("label") or f.get("name") or f.get("id") or f["marker"]
        sel = f"[data-jobpilot='{f['marker']}']"
        if action == "fill":
            if f["value"]:
                continue  # never overwrite what's there
            if key not in values:
                if f["required"]:
                    res.unknown.append(f"{label} (no confirmed value for {key})")
                continue
            if f["tag"] == "select":
                if f["required"]:
                    res.unknown.append(label)
                continue
            page.fill(sel, values[key])
            res.filled.append(f"{label} ← {key}")
        elif action == "upload":
            if f["value"]:
                continue
            if resume_path and resume_path.exists():
                page.set_input_files(sel, str(resume_path))
                res.filled.append(f"{label} ← resume file")
            else:
                res.unknown.append(f"{label} (no approved resume file)")
        elif action == "sensitive":
            if f["required"] or f["type"] not in ("checkbox", "radio"):
                res.sensitive.append(label)
        elif action == "unknown":
            res.unknown.append(label)
    res.unknown = list(dict.fromkeys(res.unknown))
    res.sensitive = list(dict.fromkeys(res.sensitive))
    if res.unknown or res.sensitive:
        res.status = "paused"
        if res.unknown:
            res.pause_reasons.append(f"{len(res.unknown)} question(s) need your answers")
        if res.sensitive:
            res.pause_reasons.append(f"{len(res.sensitive)} consent/legal/personal question(s) for you to answer")
    res.message = f"Filled {len(res.filled)} field(s). Review everything and submit yourself."
    return res


def _save_screenshot(settings: Settings, page, name: str) -> str | None:
    try:
        data = page.screenshot(full_page=True)
        return storage.store(settings, "browser", f"{name}.png", data).rel_path
    except Exception:  # pragma: no cover - best effort
        return None


def run_assist(session: Session, settings: Settings, app: Application, wait_for_user: bool = True,
               timeout_minutes: int = 45) -> BrowserRun:
    """Launch a visible browser for one application. Caller holds the 'browser' task lock."""
    run = BrowserRun(application_id=app.id, status="running")
    session.add(run)
    session.flush()
    url = app.apply_url or app.job.apply_url or app.job.source_url
    run.site = site_for(url)
    if not settings.browser_enabled:
        run.status, run.message = "failed", "Browser automation is disabled (JOBPILOT_BROWSER_ENABLED=false)"
        return _finish(session, run)
    if not url:
        run.status, run.message = "failed", "No application URL"
        return _finish(session, run)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        run.status, run.message = "failed", "Playwright not installed (pip install -e '.[browser]')"
        return _finish(session, run)

    values = profile_values(session)
    resume_path = None
    if app.resume_version and app.resume_version.file_path:
        try:
            resume_path = storage.resolve(settings, app.resume_version.file_path)
        except storage.StorageError:
            resume_path = None

    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(settings.browser_profile_dir), channel=settings.browser_channel or None,
                headless=settings.browser_headless, args=["--disable-extensions"], viewport={"width": 1280, "height": 900})
        except Exception as exc:  # browser missing / unsupported channel / profile locked
            log.warning("browser launch failed", extra={"error": str(exc)[:300]})
            run.status = "failed"
            run.message = (f"Could not start the browser ({type(exc).__name__}: {str(exc).splitlines()[0][:200]}). "
                           "Check JOBPILOT_BROWSER_CHANNEL, or use the manual workflow.")
            return _finish(session, run)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if run.site is None:
                run.status = "unsupported"
                run.message = ("Autofill is only offered on supported hosted forms (Greenhouse, Lever). "
                               "The page is open for you to complete manually.")
                res = AssistResult(status="unsupported", pause_reasons=detect_blockers(page))
            else:
                res = assist_page(page, values, resume_path)
                run.status = res.status
                run.message = res.message
                if not values:
                    run.message += " (profile not confirmed — no personal fields were filled)"
            run.filled_fields, run.unknown_fields = res.filled, res.unknown + [f"[you answer] {s}" for s in res.sensitive]
            run.pause_reasons = res.pause_reasons
            run.screenshot_path = _save_screenshot(settings, page, f"app{app.id}-run{run.id}")
            if run.status in ("paused", "unsupported"):
                review.open_item(session, "browser_paused", "application", app.id,
                                 f"Browser paused: {app.job.title}", "; ".join(res.pause_reasons) or run.message,
                                 {"run_id": run.id})
            session.commit()
            if wait_for_user:
                _watch_until_closed(session, settings, app, run, page, ctx, timeout_minutes)
        except Exception as exc:
            log.exception("browser assist failed")
            run.status, run.message = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return _finish(session, run)


def _watch_until_closed(session, settings, app, run, page, ctx, timeout_minutes) -> None:
    """Wait while you complete the form; capture a confirmation page if one appears."""
    deadline = time.monotonic() + timeout_minutes * 60
    captured = False
    while time.monotonic() < deadline:
        if page.is_closed() or not ctx.pages:
            break
        try:
            text = page.inner_text("body", timeout=2000)[:20000]
        except Exception:
            break
        if not captured and CONFIRM_TEXT.search(text):
            from jobpilot.models import ApplicationEvidence

            shot = _save_screenshot(settings, page, f"app{app.id}-confirmation")
            session.add(ApplicationEvidence(application_id=app.id, kind="confirmation_page",
                                            text=f"[auto-captured from {page.url}]\n{text[:5000]}", file_path=shot,
                                            original_filename="confirmation.png" if shot else None))
            run.message = (run.message or "") + " Confirmation page captured — confirm the submission in JobPilot."
            session.commit()
            captured = True
        time.sleep(2)
    if run.status not in ("failed",):
        run.status = "closed" if run.status == "filled" else run.status


def _finish(session: Session, run: BrowserRun) -> BrowserRun:
    run.finished_at = utcnow()
    audit.record(session, "browser.run", "application", run.application_id, actor="system", status=run.status)
    session.commit()
    return run


def manual_fields(session: Session) -> list[tuple[str, str]]:
    """Copyable confirmed values for the manual workflow."""
    labels = {"full_name": "Full name", "first_name": "First name", "last_name": "Last name", "email": "Email",
              "phone": "Phone", "location": "Location", "linkedin": "LinkedIn", "github": "GitHub",
              "website": "Website", "current_company": "Current company"}
    vals = profile_values(session)
    return [(labels[k], vals[k]) for k in labels if k in vals]


def summarize(values: dict[str, Any]) -> str:
    return ", ".join(sorted(values))
