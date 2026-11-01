"""Is a job an SDE / backend role? Decided from the title only, never guessed from the description.

Kept: software engineer / developer / SDE / SWE, backend, full stack, and developer roles named by a backend language
or stack (Java, Python, Go, Node, .NET, PHP, C++, API, microservices, distributed systems …).

Left out:

- Other job families, whatever the title also says: QA / test / SDET, support, sales, solutions engineers,
  firmware / embedded / hardware / kernel, mobile (Android, iOS, React Native, Flutter), design, data science and
  analysis, product / project roles, low-code platforms (Salesforce, SAP, AEM, ServiceNow, WordPress …), gig
  listings ("AI coding tasks", "open source contributor", talent pools, expressions of interest).
- Frontend, DevOps / SRE, data engineering and ML roles, unless the title also names backend work
  ("Java Full Stack, React/Angular" is kept, "React JS Developer" is not).
- AI-only titles ("AI Agent Developer", "AI Engineer"), unless they are plainly SDE ("Software Engineer, AI").

``check(title)`` returns ``(True, None)`` or ``(False, reason)``; ``is_sde_backend(title)`` just the boolean.
"""

from __future__ import annotations

import re

from jobpilot.services.text_utils import normalize


def _rx(words: list[str]) -> re.Pattern:
    alts = "|".join(sorted((re.escape(w).replace(r"\ ", r"\s+") for w in words), key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])")


# Another job family: never an SDE / backend role.
OTHER_FAMILY = _rx([
    "qa", "quality", "test", "tester", "testing", "sdet", "in test", "verification engineer", "validation engineer",
    "support", "helpdesk", "help desk", "escalation", "l1", "l2", "l3", "customer experience", "customer success", "customer engineer",
    "technical account",
    "sales", "presales", "pre sales", "go to market", "gtm", "account executive", "business development",
    "solutions engineer", "solution engineer", "solutions engineering", "solutions architect", "forward deployed",
    "firmware", "embedded", "hardware", "silicon", "asic", "fpga", "vlsi", "rtl", "kernel", "driver", "drivers",
    "bsp", "device", "devices", "electrical", "mechanical", "network engineer", "networking",
    "android", "ios", "mobile", "react native", "flutter", "swift", "xamarin",
    "designer", "ux", "ui ux", "graphics", "game", "games", "gameplay", "unity", "unreal",
    "data scientist", "data analyst", "business analyst", "analyst", "research scientist", "applied scientist",
    "researcher", "research engineer",
    "product manager", "product owner", "project manager", "program manager", "scrum master", "delivery manager",
    "recruiter", "talent acquisition", "technical writer", "videographer",
    "salesforce", "sap", "servicenow", "aem", "sitecore", "wordpress", "shopify", "magento", "boomi", "mulesoft",
    "power bi", "tableau", "mainframe", "cobol", "rpa", "uipath", "blue prism", "dynamics 365", "oracle apps",
    "business systems", "n8n", "zapier", "make com", "no code", "low code", "chatgpt", "langchain", "openclaw",
    "prompt", "coding tasks", "contributor", "talent pool", "expression of interest", "freelance tasks",
])

# Not backend work unless the title also names it.
NOT_BACKEND = _rx([
    "frontend", "front end", "ui", "web developer", "web designer", "web engineer", "react", "reactjs",
    "angular", "angularjs", "vue", "vuejs", "nextjs", "javascript developer",
    "devops", "dev ops", "sre", "site reliability", "reliability engineer", "mlops", "devsecops",
    "data engineer", "analytics engineer", "etl", "big data", "data warehouse",
    "ml", "machine learning", "deep learning", "computer vision", "nlp", "automation",
])

AI_ONLY = _rx(["ai", "genai", "gen ai", "generative ai", "llm", "agentic", "ai agent", "ai agents"])

# Backend work named in the title.
BACKEND = _rx([
    "backend", "back end", "server side", "fullstack", "full stack", "java", "spring", "spring boot", "kotlin",
    "python", "django", "fastapi", "flask", "golang", "go", "nodejs", "node", "dotnet", "csharp", "cpp", "rust",
    "ruby", "rails", "php", "laravel", "scala", "elixir", "api", "apis", "microservices", "distributed systems",
])

# Software engineering roles, whatever the area.
SDE = _rx([
    "software engineer", "software engineering", "software development engineer", "software dev engineer",
    "software developer", "software development", "sde", "swe", "member of technical staff", "mts",
    "computer scientist", "application developer", "applications developer", "programmer", "developer",
    "engineer i", "engineer 1",
])
STRONG_SDE = _rx(["software engineer", "software development engineer", "software dev engineer",
                  "software developer", "sde", "swe", "member of technical staff", "computer scientist"])


def _text(title: str | None) -> str:
    t = (title or "").lower()
    t = t.replace("c++", " cpp ").replace("c#", " csharp ").replace(".net", " dotnet ").replace("node.js", " nodejs ")
    t = re.sub(r"[-_/|:,()&+.]", " ", normalize(t))
    return re.sub(r"\s+", " ", t).strip()


def check(title: str | None) -> tuple[bool, str | None]:
    t = _text(title)
    if not t:
        return False, "no title"
    m = OTHER_FAMILY.search(t)
    if m:
        return False, f"not an SDE / backend role ({m.group(0)})"
    backend = bool(BACKEND.search(t))
    m = NOT_BACKEND.search(t)
    if m and not backend:
        return False, f"{m.group(0)} role, not backend"
    if AI_ONLY.search(t) and not backend and not STRONG_SDE.search(t):
        return False, "AI-only role, not SDE / backend"
    if backend or SDE.search(t):
        return True, None
    return False, "not an SDE / backend title"


def is_sde_backend(title: str | None) -> bool:
    return check(title)[0]
