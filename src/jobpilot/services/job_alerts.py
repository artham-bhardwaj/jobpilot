"""Job-alert emails (LinkedIn, Naukri, Indeed, Glassdoor, foundit, Instahyre) → jobs, for the Search agent.

Those sites don't allow automated access, so JobPilot never visits them. Instead it reads the alert emails they
already send you (Gmail, with ``JOBPILOT_GMAIL_ALLOW_READ_BODIES=true``), takes each job link with the title,
company and location shown above it, and stores the job with that link. The companies then feed board discovery,
which adds their own Greenhouse / Lever boards so the full posting is collected from the employer.

The layout of alert emails changes; this parser is deliberately simple (the lines just above each job link) and
skips anything it can't read rather than guessing.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from jobpilot.services import audit, notifications
from jobpilot.services.text_utils import clean_ws
from jobpilot.sources.base import JobData

log = logging.getLogger(__name__)

STATE_KEY = "agents.job_alerts"
SOURCE_NAME = "Job-alert emails"
QUERY = ("newer_than:3d from:(linkedin.com OR naukri.com OR indeed.com OR glassdoor.com OR glassdoor.co.in "
         "OR foundit.in OR instahyre.com)")

# (site, link pattern, canonical URL builder)
LINKS: list[tuple[str, re.Pattern, Any]] = [
    ("LinkedIn", re.compile(r"https?://(?:[a-z]+\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)[^\s>)\]]*", re.I),
     lambda m: f"https://www.linkedin.com/jobs/view/{m.group(1)}/"),
    ("Naukri", re.compile(r"https?://(?:www\.)?naukri\.com/job-listings-([a-z0-9-]+)[^\s>)\]]*", re.I),
     lambda m: f"https://www.naukri.com/job-listings-{m.group(1)}"),
    ("Indeed", re.compile(r"https?://(?:[a-z]+\.)?indeed\.com/(?:viewjob|rc/clk|pagead/clk)\?[^\s>)\]]*?jk=([a-f0-9]+)"
                          r"[^\s>)\]]*", re.I),
     lambda m: f"https://in.indeed.com/viewjob?jk={m.group(1)}"),
    ("Glassdoor", re.compile(r"https?://(?:www\.)?glassdoor\.(?:co\.in|com)/job-listing/([^\s?>)\]]+)[^\s>)\]]*", re.I),
     lambda m: f"https://www.glassdoor.co.in/job-listing/{m.group(1)}"),
    ("foundit", re.compile(r"https?://(?:www\.)?foundit\.in/job/([^\s?>)\]]+)[^\s>)\]]*", re.I),
     lambda m: f"https://www.foundit.in/job/{m.group(1)}"),
    ("Instahyre", re.compile(r"https?://(?:www\.)?instahyre\.com/job-(\d+)[^\s>)\]]*", re.I),
     lambda m: f"https://www.instahyre.com/job-{m.group(1)}"),
]
NOISE = re.compile(r"^(?:view job|apply(?: now)?|easy apply|see (?:all|more) jobs?|promoted|actively (?:recruiting|hiring)|"
                   r"\d+ (?:connections?|applicants?|alumni).*|new|be an early applicant|top applicant|save|"
                   r"job alert.*|unsubscribe.*|.*\bago)$", re.I)
URL = re.compile(r"https?://\S+")


def _context(lines: list[str], idx: int) -> list[str]:
    """The meaningful lines just above line ``idx`` (at most 3; stops at a blank gap after the first)."""
    out: list[str] = []
    j = idx - 1
    while j >= 0 and len(out) < 3:
        ln = clean_ws(URL.sub("", lines[j])).strip(" :-|•·")
        j -= 1
        if not ln:
            if out:
                break
            continue
        if NOISE.match(ln) or len(ln) > 120:
            continue
        out.insert(0, ln)
    return out


def parse(text: str) -> list[dict[str, str]]:
    """Jobs in one alert email: [{site, url, title, company, location}]. Lines with a job link whose title can't be
    read are skipped."""
    lines = (text or "").splitlines()
    found: dict[str, dict[str, str]] = {}
    for i, line in enumerate(lines):
        for site, rx, canon in LINKS:
            for m in rx.finditer(line):
                url = canon(m)
                if url in found:
                    continue
                before = clean_ws(URL.sub("", line[:m.start()])).strip(" :-|•·")
                ctx = _context(lines, i) + ([before] if before and not NOISE.match(before) else [])
                if not ctx:
                    continue
                title, company, location = (ctx + ["", ""])[:3] if len(ctx) < 3 else ctx[-3:]
                if len(title) < 3:
                    continue
                found[url] = {"site": site, "url": url, "title": title, "company": company, "location": location}
    return list(found.values())


def to_job(item: dict[str, str]) -> JobData:
    loc = item.get("location") or None
    remote = "remote" if loc and re.search(r"\bremote\b", loc, re.I) else None
    return JobData(title=item["title"][:300], company_name=(item.get("company") or None), location=loc,
                   remote_type=remote, source_url=item["url"], apply_url=item["url"],
                   external_id=item["url"], description_text=None,
                   raw={"board": "email_alert", "site": item["site"]})


def import_recent(session: Session, settings, client, max_messages: int = 25) -> dict[str, Any]:
    """Read recent alert emails and store the jobs whose titles pass your title filter. Each email is read once."""
    from jobpilot.services import jobs as job_svc
    from jobpilot.services import matching
    from jobpilot.services.regional_sources import EXCLUDE, TITLES
    from jobpilot.sources.careers import title_wanted

    state = copy.deepcopy(notifications.get_state(session, STATE_KEY) or {"seen": []})
    seen = set(state["seen"])
    source = job_svc.get_or_create_source(session, SOURCE_NAME, "manual")
    cfg = {"title_keywords": TITLES, "exclude_keywords": EXCLUDE}
    out = {"emails": 0, "links": 0, "new": 0, "skipped_title": 0, "sites": {}}
    for msg in client.list_messages(QUERY, max_results=max_messages):
        mid = msg.get("id")
        if not mid or mid in seen:
            continue
        headers, text = client.message_text(mid)
        seen.add(mid)
        state["seen"] = (state["seen"] + [mid])[-500:]
        out["emails"] += 1
        for item in parse(text):
            out["links"] += 1
            out["sites"][item["site"]] = out["sites"].get(item["site"], 0) + 1
            if not title_wanted(cfg, item["title"]):
                out["skipped_title"] += 1
                continue
            from jobpilot.services import job_delete

            data = to_job(item)
            if job_delete.is_deleted(session, source, data):
                continue
            job, outcome = job_svc.upsert(session, source, data, "email_alert")
            if outcome == "new":
                out["new"] += 1
                matching.match_job(session, job)
    notifications.set_state(session, STATE_KEY, state)
    audit.record(session, "agents.job_alerts", "job_source", source.id, actor="system", emails=out["emails"],
                 new=out["new"])
    return out


def host_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower()
