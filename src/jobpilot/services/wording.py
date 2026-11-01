"""Rule-based rewording for tailored resumes: use the job description's spelling of a skill you already list.

Only pure synonyms are swapped (e.g. "Postgres" → "PostgreSQL" when the posting says PostgreSQL). Nothing is added:
a word is replaced only by another name for the same thing, both names come from the table below, and each swap is
handed to the resume validator as an allowed correction, so any other difference still fails validation.
Versions and narrower terms are never used as synonyms ("Java" is not "Java 17", "Kubernetes" is not "EKS").
"""

from __future__ import annotations

import re

# Each group: names for exactly the same thing. Short or ambiguous words ("Go", "JS", "ML") are left out because a
# swap could change meaning; names containing LaTeX special characters (C#, C++) are left out too.
SYNONYMS: list[list[str]] = [
    ["PostgreSQL", "Postgres"],
    ["Kubernetes", "K8s"],
    ["Golang", "Go lang"],
    ["Node.js", "NodeJS", "Node JS"],
    ["React", "React.js", "ReactJS"],
    ["Next.js", "NextJS"],
    ["Vue.js", "VueJS"],
    ["Express.js", "ExpressJS"],
    ["MongoDB", "Mongo DB"],
    ["Elasticsearch", "Elastic Search"],
    ["Spring Boot", "SpringBoot"],
    ["CI/CD", "CICD"],
    ["RESTful APIs", "REST APIs"],
    ["RESTful API", "REST API"],
    ["AWS", "Amazon Web Services"],
    ["GCP", "Google Cloud Platform"],
    ["Microsoft Azure", "Azure"],
    ["Large Language Models", "LLMs"],
    ["GitHub Actions", "Github Actions"],
    ["Tailwind CSS", "TailwindCSS"],
    ["Microservices", "Micro-services"],
]


def _pattern(form: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9\\./-])" + re.escape(form) + r"(?![A-Za-z0-9+#-]|\.[A-Za-z])", re.I)


def _first_form(text: str, group: list[str]) -> str | None:
    """The form of ``group`` that appears first in ``text``, with the text's own casing (longest form wins a tie)."""
    best: tuple[int, int, str] | None = None
    for form in group:
        m = _pattern(form).search(text)
        if m and (best is None or (m.start(), -len(m.group(0))) < (best[0], best[1])):
            best = (m.start(), -len(m.group(0)), m.group(0))
    return best[2] if best else None


def plan(resume_text: str, job_text: str) -> dict[str, str]:
    """{form used in the resume: the job description's form} for every synonym group used differently."""
    out: dict[str, str] = {}
    for group in SYNONYMS:
        theirs = _first_form(job_text or "", group)
        if not theirs:
            continue
        for form in group:
            for m in _pattern(form).finditer(resume_text or ""):
                ours = m.group(0)
                if ours.lower() != theirs.lower() and not _same_form(ours, theirs, group):
                    out.setdefault(ours, _canonical_casing(theirs, group))
    # A longer form must not be shadowed by a shorter one inside it ("REST API" inside "REST APIs").
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def _same_form(a: str, b: str, group: list[str]) -> bool:
    return any(_pattern(f).fullmatch(a) and _pattern(f).fullmatch(b) for f in group)


def _canonical_casing(found: str, group: list[str]) -> str:
    """The table's casing of a form ("postgresql" in the posting → "PostgreSQL")."""
    return next((f for f in group if f.lower() == found.lower()), found)


_PROTECTED = re.compile(r"\\(?:href|url)\{[^{}]*\}")


def apply(text: str, swaps: dict[str, str]) -> tuple[str, list[tuple[str, str]]]:
    """Apply ``swaps`` to ``text`` (LaTeX or plain), never inside \\href{...} / \\url{...} targets.
    Returns the new text and one (old, new) pair per replacement."""
    done: list[tuple[str, str]] = []
    if not swaps:
        return text, done
    keep: list[str] = []

    def hide(m: re.Match) -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    work = _PROTECTED.sub(hide, text)
    for old, new in swaps.items():
        rx = re.compile(r"(?<![A-Za-z0-9\\./-])" + re.escape(old) + r"(?![A-Za-z0-9+#-]|\.[A-Za-z])")

        def sub(m: re.Match, new=new) -> str:
            done.append((m.group(0), new))
            return new

        work = rx.sub(sub, work)
    work = re.sub("\x00(\\d+)\x00", lambda m: keep[int(m.group(1))], work)
    return work, done
