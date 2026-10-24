"""How JobPilot's Telegram messages look: HTML formatting and buttons.

Formatting is applied to the plain text every message already has: everything is HTML-escaped first, then the first
line becomes a bold heading, `backtick` spans become tappable code, and "Label: value" lines get a bold label. Nothing
from the data can inject markup.

Buttons:
- ``m:<view>`` — show a view (status, jobs, questions …). Read-only.
- ``m:pause`` / ``m:resume`` — the same as /pause and /resume (automatic recruiter outreach).
- ``q:<id>`` — "Answer Q12": the bot asks for your reply (Telegram's reply box); your reply is the answer.
- ``t:a:<job>`` "I applied" → ``t:r:<job>:<resume version | 0>`` which resume; ``t:n:<job>`` not yet;
  ``t:x:<job>`` not interested (dismisses the job). ``o:<review item>:<status>`` what an employer email meant;
  ``u:<application>`` undo an update JobPilot made from an email.
- URL buttons open job postings (https only).
Every button press is checked like a command: only your chat and your user.
"""

from __future__ import annotations

import html
import re
from typing import Any

# view id -> (button label, command)
VIEWS = {
    "status": ("📊 Status", "/status"), "jobs": ("🔎 Jobs", "/jobs"), "questions": ("❓ Questions", "/questions"),
    "outcomes": ("📋 Outcomes", "/outcomes"),
    "pending": ("📝 Pending", "/pending"), "recruiters": ("👤 Recruiters", "/recruiters"),
    "outreach": ("✉️ Outreach", "/outreach"), "outreach_pending": ("📨 Emails to review", "/outreach_pending"),
    "outreach_stats": ("📈 Outreach stats", "/outreach_stats"), "today": ("🗓 Today", "/today"),
    "applications": ("📂 Applications", "/applications"), "applied": ("🗂 My applications", "/applied"),
    "track": ("🙋 Did I apply?", "/track"), "dream": ("⭐ Dream jobs", "/dream"), "alljobs": ("📜 All jobs", "/alljobs"),
}
ANSWER_PROMPT = re.compile(r"Answer Q(\d+)\b")


def _btn(view: str) -> dict:
    return {"text": VIEWS[view][0], "callback_data": f"m:{view}"}


def menu() -> dict:
    return {"inline_keyboard": [
        [_btn("status"), _btn("today")],
        [_btn("jobs"), _btn("dream")],
        [_btn("track"), _btn("applied")],
        [_btn("alljobs"), _btn("applications")],
        [_btn("questions"), _btn("outcomes")],
        [_btn("recruiters"), _btn("outreach")],
        [{"text": "⏸ Pause outreach", "callback_data": "m:pause"},
         {"text": "▶️ Resume", "callback_data": "m:resume"}],
    ]}


def reply_keyboard() -> dict:
    """The keyboard that stays under the chat's text box (each key sends the command)."""
    return {"keyboard": [["/status", "/jobs", "/dream"], ["/track", "/applied", "/questions"],
                         ["/outcomes", "/pending", "/alljobs"], ["/recruiters", "/outreach", "/menu"]],
            "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "Tap a command"}


def row(*views: str) -> list[dict]:
    return [_btn(v) for v in views if v in VIEWS]


def keyboard(*rows: list[dict]) -> dict | None:
    rows = [r for r in rows if r]
    return {"inline_keyboard": rows} if rows else None


def url_button(text: str, url: str | None) -> list[dict]:
    """Telegram only opens public https links from a button; anything else is left out (the link stays in the text)."""
    return [{"text": text[:60], "url": url}] if url and url.startswith("https://") else []


def answer_buttons(question_ids: list[int]) -> list[list[dict]]:
    ids = list(dict.fromkeys(question_ids))[:6]
    return [[{"text": f"✍️ Answer Q{q}", "callback_data": f"q:{q}"} for q in ids[i:i + 3]] for i in range(0, len(ids), 3)]


def track_buttons(job_id: int, url: str | None, label: str = "") -> list[list[dict]]:
    """Under a job: open it, then tell JobPilot whether you applied."""
    p = f"{label} " if label else ""
    return [r for r in (url_button(f"🔗 {p}Apply / open posting", url),
                        [{"text": f"✅ {p}I applied", "callback_data": f"t:a:{job_id}"},
                         {"text": "⏳ Not yet", "callback_data": f"t:n:{job_id}"},
                         {"text": "🚫 Not interested", "callback_data": f"t:x:{job_id}"}]) if r]


def resume_buttons(session, job_id: int) -> dict:
    from jobpilot.models import ResumeVersion
    from sqlalchemy import select

    versions = list(session.scalars(select(ResumeVersion).where(ResumeVersion.status == "approved")
                                    .order_by(ResumeVersion.id.desc()).limit(6)))
    rows = [[{"text": f"📄 {v.resume.name} v{v.version_no}"[:60], "callback_data": f"t:r:{job_id}:{v.id}"}]
            for v in versions]
    rows.append([{"text": "📄 Another resume / don't remember", "callback_data": f"t:r:{job_id}:0"}])
    return {"inline_keyboard": rows}


OUTCOMES = [("❌ Rejected", "rejected"), ("📝 Assessment", "assessment"), ("🗓 Interview", "interview"),
            ("🎉 Offer", "offer"), ("🤷 Nothing changed", "none")]


def outcome_buttons(review_id: int, guess: str | None = None) -> list[list[dict]]:
    btns = [{"text": (f"{t} (likely)" if v == guess else t), "callback_data": f"o:{review_id}:{v}"} for t, v in OUTCOMES]
    return [btns[:3], btns[3:]]


def force_reply(qid: int) -> dict:
    return {"force_reply": True, "input_field_placeholder": f"Your answer for Q{qid}"[:64]}


# ---- formatting ----------------------------------------------------------------------------------------------

_LABEL = re.compile(r"^(\s*)([A-Z][A-Za-z /()'&-]{1,40}):(\s)")


def to_html(text: str, heading: bool = True) -> str:
    out = []
    for i, line in enumerate((text or "").split("\n")):
        esc = html.escape(line, quote=False)
        esc = re.sub(r"`([^`\n]{1,300})`", r"<code>\1</code>", esc)
        if i == 0 and heading and esc.strip():
            esc = f"<b>{esc}</b>"
        elif not esc.lstrip().startswith(("•", "-", "➡", "<code>")):
            esc = _LABEL.sub(r"\1<b>\2:</b>\3", esc, count=1)
        out.append(esc)
    return "\n".join(out)


# ---- buttons for notifications -------------------------------------------------------------------------------

def notification_markup(kind: str, events: list[dict[str, Any]]) -> dict | None:
    from jobpilot.services.answers import is_legal

    if kind == "high_match":
        e = events[0]
        applied = [[{"text": "✅ I applied", "callback_data": f"t:a:{e['job_id']}"},
                    {"text": "🚫 Not interested", "callback_data": f"t:x:{e['job_id']}"}]] if e.get("job_id") else []
        return keyboard(url_button("🔗 Open posting", e.get("url")), *applied, row("jobs", "applied"))
    if kind == "apply_check":
        e = events[0]
        return keyboard(*track_buttons(e["job_id"], e.get("url")), row("track", "applied"))
    if kind == "email_outcome":
        e = events[0]
        if e.get("undo"):
            return keyboard([{"text": "↩️ Undo", "callback_data": f"u:{e['app_id']}"}], row("applied", "outcomes"))
        return keyboard(*outcome_buttons(e["review_id"], e.get("guess")), row("applied"))
    if kind == "relevant_digest":
        return keyboard(row("jobs", "today"))
    if kind == "questions":
        ids = [e["question_id"] for e in events if e.get("question_id") and not is_legal(e.get("question") or "")]
        return keyboard(*answer_buttons(ids), row("questions", "pending"))
    if kind == "application_status":
        return keyboard(row("outcomes", "applications"))
    if kind in ("recruiter_needed", "recruiter_found", "bounce"):
        return keyboard(row("recruiters", "outreach_pending"))
    if kind in ("outreach_sent", "outreach_gmail", "reply", "follow_up", "draft_review"):
        return keyboard(row("outreach", "outreach_pending"))
    if kind in ("task_failure", "source_failure"):
        return keyboard(row("status"))
    if kind == "daily_summary":
        return menu()
    return keyboard(row("status", "today"))


# ---- buttons for command replies -----------------------------------------------------------------------------

def command_markup(session, command: str | None) -> dict | None:
    if command in ("/start", "/help", "/menu", "/status", "/today"):
        return menu()
    if command == "/questions":
        from jobpilot.models import ApplicationQuestion
        from jobpilot.services.answers import is_legal
        from jobpilot.services.applications import OPEN_STATES
        from sqlalchemy import select

        ids = [q.id for q in session.scalars(select(ApplicationQuestion).where(ApplicationQuestion.status == "unanswered")
                                             .order_by(ApplicationQuestion.id))
               if q.application and q.application.status in OPEN_STATES and not is_legal(q.question)]
        return keyboard(*answer_buttons(ids), row("pending", "status"))
    if command == "/jobs":
        from jobpilot.models import Job
        from jobpilot.services import settings_store
        from sqlalchemy import select

        threshold = settings_store.get(session, "matching")["relevance_threshold"]
        jobs = list(session.scalars(select(Job).where(Job.status == "active", Job.user_status != "dismissed",
                                                      Job.relevance_score >= threshold, Job.eligibility != "ineligible")
                                    .order_by(Job.relevance_score.desc()).limit(3)))
        links = [url_button(f"🔗 {j.title[:40]} — {j.company_name or ''}"[:60], j.apply_url or j.source_url) for j in jobs]
        return keyboard(*links, row("questions", "status"))
    if command == "/pending":
        return keyboard(row("questions", "outreach_pending"))
    if command in ("/outcomes", "/applications", "/applied", "/company", "/cooldown"):
        return keyboard(row("applied", "track"), row("outcomes", "dream"))
    if command in ("/track", "/dream"):
        from jobpilot.services import tracker

        jobs = tracker.to_ask(session, limit=3) if command == "/track" else \
            [j for j in tracker.relevant_jobs(session) if tracker.is_dream(session, j.company_name)][:3]
        rows = [r for i, j in enumerate(jobs, 1) for r in track_buttons(j.id, j.apply_url or j.source_url, str(i))]
        return keyboard(*rows, row("applied", "alljobs"))
    if command == "/alljobs":
        return keyboard(row("track", "dream"), row("applied"))
    if command and command.startswith("t:a:") and command[4:].isdigit():
        return resume_buttons(session, int(command[4:]))
    if command in ("/recruiters", "/outreach", "/outreach_pending", "/outreach_stats"):
        return keyboard(row("recruiters", "outreach"), row("outreach_pending", "outreach_stats"))
    if command in ("/pause", "/resume"):
        return keyboard(row("status", "outreach"))
    return keyboard(row("status")) if command else None
