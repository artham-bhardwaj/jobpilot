"""Skill dictionary used to extract skills from job descriptions and resumes.

Canonical name -> (category, aliases). Matching is whole-word on normalised
text. Extend freely; unknown skills simply aren't detected (they are never
invented).
"""

from __future__ import annotations

import re

from jobpilot.services.text_utils import contains_term, normalize, normalize_company

SKILLS: dict[str, tuple[str, list[str]]] = {
    # Languages
    "java": ("language", ["java", "java 8", "java 11", "java 17", "java 21", "core java", "j2ee", "java ee"]),
    "python": ("language", ["python", "python3", "python 3"]),
    "javascript": ("language", ["javascript", "js", "ecmascript"]),
    "typescript": ("language", ["typescript"]),
    "go": ("language", ["go", "golang", "go lang"]),  # bare "Go" only in a technical context (_ambiguous_ok)
    "c++": ("language", ["c++", "cpp"]),
    "c#": ("language", ["c#", "csharp", ".net", "dotnet"]),
    "kotlin": ("language", ["kotlin"]),
    "scala": ("language", ["scala"]),
    "rust": ("language", ["rust"]),
    "ruby": ("language", ["ruby", "ruby on rails", "rails"]),
    "php": ("language", ["php"]),
    "sql": ("database", ["sql", "t-sql", "pl/sql", "plsql"]),
    "bash": ("tool", ["bash", "shell scripting", "shell script"]),
    # Java ecosystem
    "spring boot": ("framework", ["spring boot", "springboot", "spring-boot"]),
    "spring": ("framework", ["spring framework", "spring mvc", "spring"]),
    "spring cloud": ("framework", ["spring cloud"]),
    "spring security": ("framework", ["spring security"]),
    "hibernate": ("framework", ["hibernate", "jpa", "spring data jpa"]),
    "maven": ("tool", ["maven"]),
    "gradle": ("tool", ["gradle"]),
    "junit": ("testing", ["junit", "mockito"]),
    "jvm": ("concept", ["jvm", "jvm tuning", "garbage collection"]),
    "multithreading": ("concept", ["multithreading", "multi-threading", "concurrency", "concurrent programming"]),
    # Python ecosystem
    "django": ("framework", ["django", "django rest framework", "drf"]),
    "fastapi": ("framework", ["fastapi"]),
    "flask": ("framework", ["flask"]),
    "celery": ("tool", ["celery"]),
    "pytest": ("testing", ["pytest"]),
    "sqlalchemy": ("framework", ["sqlalchemy"]),
    "pandas": ("library", ["pandas"]),
    "numpy": ("library", ["numpy"]),
    # Web / API
    "rest api": ("concept", ["rest api", "rest apis", "restful", "rest", "restful apis", "restful services"]),
    "graphql": ("concept", ["graphql"]),
    "grpc": ("concept", ["grpc"]),
    "microservices": ("concept", ["microservices", "microservice", "micro-services", "microservice architecture"]),
    "react": ("framework", ["react", "react.js", "reactjs"]),
    "node.js": ("framework", ["node.js", "nodejs", "node"]),
    "html/css": ("language", ["html", "css"]),
    # Data stores
    "postgresql": ("database", ["postgresql", "postgres"]),
    "mysql": ("database", ["mysql"]),
    "oracle db": ("database", ["oracle database", "oracle db"]),
    "mongodb": ("database", ["mongodb", "mongo"]),
    "redis": ("database", ["redis"]),
    "cassandra": ("database", ["cassandra"]),
    "dynamodb": ("database", ["dynamodb"]),
    "elasticsearch": ("database", ["elasticsearch", "elastic search", "opensearch"]),
    "nosql": ("database", ["nosql"]),
    # Messaging / streaming
    "kafka": ("tool", ["kafka", "apache kafka"]),
    "rabbitmq": ("tool", ["rabbitmq"]),
    "spark": ("tool", ["spark", "pyspark", "apache spark"]),
    "airflow": ("tool", ["airflow"]),
    # Cloud / DevOps
    "aws": ("cloud", ["aws", "amazon web services", "ec2", "s3", "lambda"]),
    "gcp": ("cloud", ["gcp", "google cloud", "google cloud platform"]),
    "azure": ("cloud", ["azure", "microsoft azure"]),
    "docker": ("devops", ["docker", "containers", "containerization"]),
    "kubernetes": ("devops", ["kubernetes", "k8s", "eks", "gke"]),
    "terraform": ("devops", ["terraform"]),
    "ci/cd": ("devops", ["ci/cd", "ci cd", "continuous integration", "jenkins", "github actions", "gitlab ci"]),
    "linux": ("tool", ["linux", "unix"]),
    "git": ("tool", ["git", "github", "gitlab", "version control"]),
    "observability": ("devops", ["prometheus", "grafana", "observability", "monitoring"]),
    # CS fundamentals
    "data structures": ("concept", ["data structures", "data structure"]),
    "algorithms": ("concept", ["algorithms", "algorithm design"]),
    "oop": ("concept", ["oop", "object oriented", "object-oriented", "object oriented programming"]),
    "system design": ("concept", ["system design", "distributed systems", "scalable systems", "high level design"]),
    "low level design": ("concept", ["low level design", "lld", "design patterns"]),
    "operating systems": ("concept", ["operating systems"]),
    "computer networks": ("concept", ["computer networks", "networking", "tcp/ip"]),
    "dbms": ("concept", ["dbms", "database design", "data modeling", "data modelling"]),
    "unit testing": ("testing", ["unit testing", "unit tests", "test driven development", "tdd"]),
    "agile": ("process", ["agile", "scrum"]),
    # AI / ML
    "machine learning": ("ml", ["machine learning", "ml"]),
    "deep learning": ("ml", ["deep learning", "neural networks", "neural network"]),
    "pytorch": ("ml", ["pytorch", "torch"]),
    "tensorflow": ("ml", ["tensorflow", "keras"]),
    "scikit-learn": ("ml", ["scikit-learn", "sklearn", "scikit learn"]),
    "nlp": ("ml", ["nlp", "natural language processing"]),
    "computer vision": ("ml", ["computer vision", "opencv"]),
    "llm": ("ml", ["llm", "llms", "large language models", "large language model", "generative ai", "genai"]),
    "rag": ("ml", ["rag", "retrieval augmented generation", "retrieval-augmented generation", "vector database"]),
    "transformers": ("ml", ["transformers", "hugging face", "huggingface"]),
    "mlops": ("ml", ["mlops", "mlflow", "kubeflow", "model deployment"]),
    "statistics": ("ml", ["statistics", "statistical modeling", "probability"]),
}

BUILTIN_SKILLS = dict(SKILLS)
_INDEX: list[tuple[str, str]] = []
_ACTIVE_KEY: str | None = None


def _rebuild(skills: dict[str, tuple[str, list[str]]]) -> None:
    global _INDEX
    _INDEX = sorted(((alias, canon) for canon, (_, aliases) in skills.items() for alias in aliases),
                    key=lambda x: -len(x[0]))


def configure(extra: dict | None = None, disabled: list[str] | None = None) -> None:
    """Apply your dictionary customisations on top of the built-in skills.

    ``extra``: {"canonical name": {"category": "...", "aliases": ["...", ...]}} (new skills or extra aliases);
    ``disabled``: canonical names to stop recognising.
    """
    import json

    global _ACTIVE_KEY
    key = json.dumps([extra or {}, sorted(disabled or [])], sort_keys=True)
    if key == _ACTIVE_KEY:
        return
    merged = {k: (cat, list(al)) for k, (cat, al) in BUILTIN_SKILLS.items()}
    for name, spec in (extra or {}).items():
        canon = normalize(name)
        if not canon:
            continue
        cat, aliases = merged.get(canon, (spec.get("category") or "custom", []))
        aliases = list(dict.fromkeys(aliases + [canon] + [normalize(a) for a in spec.get("aliases", []) if normalize(a)]))
        merged[canon] = (spec.get("category") or cat, aliases)
    for name in disabled or []:
        merged.pop(normalize(name), None)
    SKILLS.clear()
    SKILLS.update(merged)
    _rebuild(SKILLS)
    _ACTIVE_KEY = key


def configure_from_settings(session) -> None:
    from jobpilot.services import settings_store

    cfg = settings_store.get(session, "skill_dictionary")
    configure(cfg.get("extra"), cfg.get("disabled"))


_rebuild(SKILLS)
_AMBIGUOUS = {"go", "rest", "node", "ml", "torch", "spring", "rust", "js", "s3", "lambda"}


def canonical(name: str) -> str:
    """Map a free-form skill name to its canonical form (or the normalised input)."""
    n = normalize(name)
    for alias, canon in _INDEX:
        if normalize(alias) == n:
            return canon
    return n


def category_of(name: str) -> str | None:
    entry = SKILLS.get(canonical(name))
    return entry[0] if entry else None


def extract_skills(text: str | None, extra_terms: list[str] | None = None, ignore: list[str] | None = None) -> list[str]:
    """Return canonical skills mentioned in ``text`` (ordered by first occurrence).

    ``ignore``: company names blanked out before matching (suffixes like "Inc." dropped), e.g. the employer's own
    name: "GitLab" in GitLab's postings is not a git requirement.
    """
    if not text:
        return []
    norm = normalize(text)
    for term in ignore or []:
        t = normalize_company(term)
        if len(t) >= 3:
            norm = re.sub(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9+#])", lambda m: " " * len(m.group(0)), norm)
    found: dict[str, int] = {}
    # Longest aliases first; matched spans are blanked so "spring boot" doesn't also count as "spring".
    for alias, canon in _INDEX:
        if alias in _AMBIGUOUS and not _ambiguous_ok(alias, text):
            continue
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(normalize(alias)) + r"(?![a-z0-9+#])")
        m = pattern.search(norm)
        if not m:
            continue
        found.setdefault(canon, m.start())
        found[canon] = min(found[canon], m.start())
        norm = pattern.sub(lambda mm: " " * len(mm.group(0)), norm)
    for term in extra_terms or []:
        c = canonical(term)
        if c not in found and contains_term(norm, term):
            found[c] = norm.find(normalize(term))
    return [k for k, _ in sorted(found.items(), key=lambda kv: kv[1])]


def _ambiguous_ok(alias: str, original: str) -> bool:
    """Ambiguous short words only count in a technical context."""
    if alias == "go":
        return re.search(r"\b(Go|Golang)\b(?!\s+(?:to|through|beyond|above|live))", original) is not None and \
            re.search(r"\bGo\b", original) is not None
    if alias in ("rest",):
        return re.search(r"\bREST\b", original) is not None
    if alias == "ml":
        return re.search(r"\bML\b", original) is not None
    if alias == "node":
        return re.search(r"\bNode(\.js)?\b", original) is not None
    if alias == "spring":
        return re.search(r"\bSpring\b", original) is not None
    if alias == "rust":
        return re.search(r"\bRust\b", original) is not None
    if alias == "js":
        return re.search(r"\bJS\b", original) is not None
    if alias in ("s3", "lambda"):
        return re.search(r"\b(AWS|S3|Lambda)\b", original) is not None and "aws" in original.lower()
    if alias == "torch":
        return re.search(r"\b[Tt]orch\b", original) is not None
    return True
