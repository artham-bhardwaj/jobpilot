"""Where a job is: India, Dubai / UAE, remote (open to you), or somewhere else.

A job can be in more than one region ("Bengaluru, India; Remote" is both India and remote). Tags are stored on the
job as ``,india,remote,`` so a region filter is a simple ``LIKE '%,india,%'``.

Remote counts only when the posting doesn't restrict it to somewhere else: "Remote", "Remote - India",
"Remote, APAC", "Anywhere" are remote for you; "Remote - US" or "Remote, Germany" are not (they are tagged
``other``). Matching is on whole words of the location and title, never guessed from the description.
"""

from __future__ import annotations

import re

from jobpilot.services.text_utils import normalize

REGIONS = ("india", "remote", "dubai")
LABELS = {"india": "India", "remote": "Remote", "dubai": "Dubai / UAE", "other": "Elsewhere"}

INDIA = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "navi mumbai", "thane",
         "noida", "gurugram", "gurgaon", "delhi", "new delhi", "ncr", "kolkata", "ahmedabad", "kochi", "cochin",
         "trivandrum", "thiruvananthapuram", "coimbatore", "jaipur", "chandigarh", "mohali", "indore", "nagpur",
         "vadodara", "visakhapatnam", "bhubaneswar", "lucknow", "mysore", "mysuru", "mangalore", "karnataka",
         "telangana", "maharashtra", "tamil nadu", "uttar pradesh", "haryana", "kerala", "west bengal", "gujarat")
DUBAI = ("dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "uae", "u a e", "united arab emirates",
         "emirates", "difc", "dubai internet city")
REMOTE = ("remote", "work from home", "wfh", "anywhere", "worldwide", "fully distributed", "distributed")
# Remote but open to you: India / UAE themselves, or a wide area that includes them.
OPEN_TO_YOU = ("apac", "asia", "asia pacific", "emea", "middle east", "mena", "gcc", "global", "worldwide",
               "anywhere", "any location", "all locations", "international")
# Places that, named alone on a remote job, mean it is not open from India or the UAE.
ELSEWHERE = ("us", "usa", "u s", "united states", "america", "americas", "north america", "latam", "latin america",
             "south america", "canada", "mexico", "brazil", "argentina", "colombia", "chile", "peru", "uk",
             "united kingdom", "england", "scotland", "ireland", "europe", "eu", "european union", "cet", "est",
             "pst", "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium", "poland",
             "romania", "ukraine", "sweden", "norway", "denmark", "finland", "switzerland", "austria", "czechia",
             "czech republic", "hungary", "greece", "turkey", "turkiye", "israel", "egypt", "nigeria", "kenya",
             "south africa", "australia", "new zealand", "japan", "korea", "south korea", "china", "taiwan",
             "hong kong", "singapore", "philippines", "vietnam", "thailand", "malaysia", "indonesia", "pakistan",
             "bangladesh", "sri lanka", "nepal", "saudi arabia", "qatar", "bahrain", "kuwait", "oman", "jordan",
             "lebanon", "uk only", "us only",
             # cities often named alone
             "london", "manchester", "dublin", "berlin", "munich", "paris", "amsterdam", "madrid", "barcelona",
             "lisbon", "warsaw", "stockholm", "zurich", "new york", "nyc", "san francisco", "sf", "bay area",
             "seattle", "austin", "boston", "chicago", "los angeles", "denver", "atlanta", "washington", "toronto",
             "vancouver", "montreal", "sydney", "melbourne", "tokyo", "seoul", "shanghai", "beijing", "sao paulo",
             "mexico city", "buenos aires", "karachi", "lahore", "cairo", "riyadh", "doha")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[./]", " ", normalize(text or ""))).strip()


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text) for w in words)


def classify(location: str | None, remote_type: str | None = None, title: str | None = None) -> list[str]:
    """The regions of a job, in REGIONS order; ``["other"]`` when none applies."""
    loc = _norm(location)
    text = f"{loc} {_norm(title)}"
    out = []
    india, dubai = _has(text, INDIA), _has(text, DUBAI)
    if india:
        out.append("india")
    if remote_type == "remote" or _has(loc, REMOTE):
        # "Remote" alone, or remote somewhere that includes India / the UAE. Remote restricted to other places only
        # is not remote for you.
        if india or dubai or _has(loc, OPEN_TO_YOU) or not _has(loc, ELSEWHERE):
            out.append("remote")
    if dubai:
        out.append("dubai")
    return out or ["other"]


def tags(location: str | None, remote_type: str | None = None, title: str | None = None) -> str:
    return "," + ",".join(classify(location, remote_type, title)) + ","


def of(job) -> list[str]:
    """The stored tags of a job (computed if missing)."""
    raw = getattr(job, "regions", None) or tags(job.location, job.remote_type, job.title)
    return [r for r in raw.strip(",").split(",") if r]


def remote_elsewhere(job) -> bool:
    """A remote job restricted to places other than India / the UAE."""
    return job.remote_type == "remote" and of(job) == ["other"]


def backfill(session, only_missing: bool = True) -> int:
    """Set ``regions`` on jobs (all, or those without tags). Returns how many changed."""
    from sqlalchemy import select

    from jobpilot.models import Job

    stmt = select(Job)
    if only_missing:
        stmt = stmt.where(Job.regions.is_(None))
    n = 0
    for job in session.scalars(stmt):
        value = tags(job.location, job.remote_type, job.title)
        if job.regions != value:
            job.regions = value
            n += 1
    return n


def counts(session, status: str = "active") -> dict[str, int]:
    """How many (not dismissed) jobs are in each region."""
    from sqlalchemy import func, select

    from jobpilot.models import Job

    base = select(func.count(Job.id)).where(Job.user_status != "dismissed")
    if status:
        base = base.where(Job.status == status)
    out = {"all": session.scalar(base) or 0}
    for r in (*REGIONS, "other"):
        out[r] = session.scalar(base.where(Job.regions.like(f"%,{r},%"))) or 0
    return out


def overview(session, per_region: int = 5, new_days: int = 7) -> list[dict]:
    """For the dashboard: per region, active fresher / SDE-1 job count, how many are new this week, and the best matches."""
    from datetime import timedelta

    from sqlalchemy import func, or_, select

    from jobpilot.models import Job, utcnow

    since = utcnow() - timedelta(days=new_days)
    out = []
    for r in REGIONS:
        # Same focus as the Jobs page default: fresher / SDE-1 (or level not stated), in your role categories.
        base = select(Job).where(Job.status == "active", Job.user_status != "dismissed",
                                 Job.regions.like(f"%,{r},%"), or_(Job.level_fit.is_(None), Job.level_fit != "no"),
                                 Job.category.is_not(None))
        count = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        new = session.scalar(select(func.count()).select_from(base.where(Job.first_seen_at >= since).subquery())) or 0
        top = list(session.scalars(base.order_by(Job.relevance_score.desc().nulls_last(), Job.first_seen_at.desc())
                                   .limit(per_region)))
        out.append({"key": r, "label": LABELS[r], "count": count, "new": new, "top": top})
    return out
