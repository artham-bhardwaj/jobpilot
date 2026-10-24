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
    "ashby": re.compile(r"(^|\.)jobs\.ashbyhq\.com$"),
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
    ("country", re.compile(r"^country\b|country of residence|current country|which country do you (live|reside)", re.I)),
    ("location", re.compile(r"^location$|current location|city|where are you (based|located)", re.I)),
    ("current_company", re.compile(r"current (company|employer)|^org$|organi[sz]ation", re.I)),
]
RESUME_FIELD = re.compile(r"resume|cv|curriculum", re.I)

COLLECT_FIELDS_JS = """
() => {
  const out = [];
  const els = Array.from(document.querySelectorAll('input, select, textarea, [contenteditable="true"], [contenteditable=""]'));
  window.__jpSeq = window.__jpSeq || 0;
  els.forEach((el) => {
    const editable = el.isContentEditable && !['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName);
    const type = editable ? 'contenteditable' : (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    // Searchable dropdowns (react-select, used by Greenhouse) keep a nameless, unfocusable "required" proxy input
    // next to the real combobox: the combobox is the field.
    if (!el.name && !el.id && el.tabIndex === -1 && el.getAttribute('role') !== 'combobox') return;
    const style = window.getComputedStyle(el);
    const visible = type === 'file' || (style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null);
    if (!visible) return;
    const combobox = el.getAttribute('role') === 'combobox';
    let label = '';
    const byIds = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)
      .map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim();
    if (byIds) label = byIds;
    if (!label && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
    if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    if (!label) { const c = el.closest('.field, .application-question, .form-group, li, div'); if (c) label = (c.innerText || '').slice(0, 200); }
    // stable across re-scans, so fields rendered later (after an answer) get new markers and old ones keep theirs
    const marker = el.getAttribute('data-jobpilot') || ('jp-' + (window.__jpSeq++));
    el.setAttribute('data-jobpilot', marker);
    let value = editable ? (el.innerText || '').trim() : (el.type === 'file' ? (el.files.length ? 'file' : '') : (el.value || ''));
    if (combobox && !value) {  // the chosen option is shown next to the input, not in it
      const shown = el.closest('[class*="value-container"], [class*="control"]');
      const single = shown && shown.querySelector('[class*="single-value"], [class*="singleValue"]');
      value = single ? single.innerText.trim() : '';
    }
    out.push({marker, tag: el.tagName.toLowerCase(), type: combobox ? 'combobox' : type, combobox, name: el.name || '',
              id: el.id || '', label: (label || '').trim().slice(0, 200),
              required: el.required || el.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test((label || '').trim()),
              value, checked: !!el.checked});
  });
  return out;
}
"""


REPORT_SECTIONS = ("filled_verified", "required_missing", "awaiting_answer", "legal_decision", "optional_blank",
                   "unverified")


@dataclass
class AssistResult:
    status: str = "filled"  # filled | paused | unsupported | failed
    filled: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)
    pause_reasons: list[str] = field(default_factory=list)
    message: str = ""
    # filled_verified / required_missing / awaiting_answer / legal_decision / optional_blank / unverified:
    # [{"field", "detail"}]
    report: dict[str, list[dict]] = field(default_factory=lambda: {k: [] for k in REPORT_SECTIONS})


def site_for(url: str | None) -> str | None:
    host = urlsplit(url or "").hostname or ""
    for name, rx in SUPPORTED_HOSTS.items():
        if rx.search(host):
            return name
    return None


def classify(f: dict) -> tuple[str, str | None]:
    """Decide what to do with a form field: ('fill', key) | ('upload', 'resume') | ('sensitive'|'unknown'|'skip', None)."""
    text = " ".join(x for x in (f.get("label"), f.get("name"), f.get("id")) if x)
    if f["type"] == "file":  # only the resume is ever attached; another upload (cover letter) is fine to leave empty
        return ("upload", "resume") if RESUME_FIELD.search(text) else (("unknown", None) if f.get("required")
                                                                       else ("skip", None))
    if SENSITIVE.search(text) or f["type"] in ("checkbox", "radio"):
        return "sensitive", None
    if f["type"] == "password":
        return "sensitive", None
    # The label decides; name/id only when the label names no profile field. More than one match: not guessed.
    label = re.sub(r"\s*\*\s*$", "", (f.get("label") or "").strip())
    hits = list(dict.fromkeys(key for key, rx in FIELD_RULES if rx.search(label)))
    if not hits:
        hits = list(dict.fromkeys(key for key, rx in FIELD_RULES
                                  if rx.search(f.get("name") or "") or rx.search(f.get("id") or "")))
    if len(hits) == 1:
        return "fill", hits[0]
    if len(hits) > 1:
        return "ambiguous", ", ".join(hits)
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
    country = profile_country(p.current_location)
    if country:
        vals["country"] = country
    clean, _ = validate_values({k: v for k, v in vals.items() if v},
                               employers={current.company} if current else set())
    return clean


COUNTRIES = ("India", "United States", "United Kingdom", "Canada", "Germany", "Ireland", "Singapore", "Australia",
             "Netherlands", "France", "Japan", "United Arab Emirates")
# Unambiguous Indian cities: a profile location of just "Thane" or "Pune" means India.
INDIAN_CITIES = {"mumbai", "thane", "navi mumbai", "pune", "bengaluru", "bangalore", "hyderabad", "chennai", "delhi",
                 "new delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad", "jaipur", "kochi", "indore",
                 "chandigarh", "coimbatore", "nagpur", "lucknow", "bhubaneswar", "trivandrum", "thiruvananthapuram",
                 "mysuru", "mysore", "vadodara", "surat", "visakhapatnam", "dehradun", "mohali", "ghaziabad"}


def profile_country(location: str | None) -> str | None:
    """Country from your confirmed location, only when it is stated or unambiguous; otherwise None (never guessed)."""
    loc = (location or "").strip()
    for c in COUNTRIES:
        if re.search(rf"(?<![a-z]){re.escape(c)}(?![a-z])", loc, re.I):
            return c
    city = loc.split(",")[0].strip().lower()
    return "India" if city in INDIAN_CITIES else None


_VALIDATORS = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "phone": re.compile(r"^\+?[\d\s().-]{7,20}$"),
    "linkedin": re.compile(r"^https?://\S+$"), "github": re.compile(r"^https?://\S+$"),
    "website": re.compile(r"^https?://\S+$"),
    "first_name": re.compile(r"^[^\W\d_][\w .'’-]{0,79}$"), "last_name": re.compile(r"^[^\W\d_][\w .'’-]{0,79}$"),
    "full_name": re.compile(r"^[^\W\d_][\w .'’-]{0,159}$"),
}


def validate_values(values: dict[str, str], employers: set[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Values safe to type: trimmed, no control characters, the right shape, and (employer) exactly a confirmed
    current employer. Anything malformed is dropped and reported instead of being entered."""
    clean, problems = {}, []
    for key, raw in values.items():
        v = (raw or "").strip()
        if not v:
            continue
        if any(ord(ch) < 32 for ch in v) or len(v) > 300:
            problems.append(f"{key}: unusable value")
            continue
        rx = _VALIDATORS.get(key)
        if rx and not rx.match(v):
            problems.append(f"{key}: value does not look like a {key.replace('_', ' ')}")
            continue
        if key == "current_company" and employers is not None and v not in employers:
            problems.append("current_company: not a confirmed current employer")
            continue
        clean[key] = v
    return clean, problems


CAPTCHA_MARKUP_JS = """
() => {
  const srcs = Array.from(document.querySelectorAll('iframe[src], script[src]')).map(e => e.getAttribute('src') || '');
  const widgets = document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #recaptcha, [id^="arkose"]');
  return {srcs, widgets: widgets.length};
}
"""


CAPTCHA_REASON = "CAPTCHA / bot check present — complete it yourself"
# Score-based checks that show no puzzle (reCAPTCHA v3 / Enterprise invisible). Still an anti-bot control: JobPilot
# never submits through one unattended; the assisted workflow (you click Submit) is the way through.
INVISIBLE_REASON = ("CAPTCHA / bot check present (invisible, score-based reCAPTCHA; no puzzle shown) — JobPilot does "
                    "not submit through anti-bot checks; open it with 'jobpilot assist' and submit it yourself")
_INVISIBLE_SRC = re.compile(r"recaptcha/(?:enterprise|api)\.js\?(?:.*&)?render=(?!explicit)|[?&]size=invisible", re.I)


def detect_blockers(page) -> list[str]:
    reasons = []
    urls = [frame.url for frame in page.frames if frame.url and BLOCKER_IFRAME.search(frame.url)]
    # The page's own markup too, so a challenge counts even if its iframe/script hasn't loaded (slow or blocked network).
    try:
        markup = page.evaluate(CAPTCHA_MARKUP_JS)
    except Exception:
        markup = {"srcs": [], "widgets": 0}
    urls += [src for src in markup["srcs"] if BLOCKER_IFRAME.search(src)]
    if urls or markup["widgets"]:
        invisible = urls and not markup["widgets"] and all(_INVISIBLE_SRC.search(u) or "recaptcha" in u.lower()
                                                            for u in urls) and any(_INVISIBLE_SRC.search(u) for u in urls)
        reasons.append(INVISIBLE_REASON if invisible else CAPTCHA_REASON)
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


TEXT_TYPES = {"text", "textarea", "email", "tel", "url", "number", "input", "contenteditable"}

# Radio buttons are handled per group (one question, several options), never one by one.
RADIO_GROUPS_JS = """
() => {
  const groups = {};
  for (const el of document.querySelectorAll('input[type=radio]')) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const key = el.name || ('jp-noname-' + Math.random());
    const g = groups[key] || (groups[key] = {name: el.name, options: [], required: false, checked: false, question: ''});
    let label = '';
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.innerText; }
    if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
    const marker = 'jp-radio-' + Object.keys(groups).indexOf(key) + '-' + g.options.length;
    el.setAttribute('data-jobpilot-radio', marker);
    g.options.push({marker, label: (label || el.value || '').trim().slice(0, 120)});
    g.required = g.required || el.required || el.getAttribute('aria-required') === 'true';
    g.checked = g.checked || el.checked;
    if (!g.question) {
      const set = el.closest('fieldset, [role=radiogroup]');
      const legend = set && (set.querySelector('legend') || (set.getAttribute('aria-labelledby') &&
                     document.getElementById(set.getAttribute('aria-labelledby'))));
      g.question = ((legend && legend.innerText) || (set && set.getAttribute('aria-label')) || '').trim().slice(0, 200);
      if (set && set.getAttribute('aria-required') === 'true') g.required = true;
    }
  }
  return Object.values(groups).map(g => ({...g, required: g.required || /\\*\\s*$/.test(g.question)}));
}
"""


def _radio_plan(group: dict, answer_for) -> tuple[str, dict | None]:
    """('choose', option) when exactly one option equals the approved answer; otherwise why not."""
    from jobpilot.services.answers import is_legal

    question = group["question"] or group["name"] or "radio question"
    sensitive = bool(SENSITIVE.search(question))
    if is_legal(question):
        return "sensitive", None  # an attestation: yours on the form, never from a stored answer
    answer = answer_for(question, sensitive) if answer_for else None
    if not answer:
        return ("sensitive" if sensitive else "unknown"), None
    want = " ".join(answer.split()).lower()
    hits = [o for o in group["options"] if " ".join(o["label"].split()).lower() == want]
    return ("choose", hits[0]) if len(hits) == 1 else ("no_exact_option", None)


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


def _shown_choice(page, sel: str) -> str:
    """What a searchable drop-down shows as chosen (the input itself is cleared after a choice)."""
    return page.evaluate("""(s) => { const el = document.querySelector(s);
        const c = el && el.closest('[class*="value-container"], [class*="control"]');
        const single = c && c.querySelector('[class*="single-value"], [class*="singleValue"]');
        return single ? single.innerText : (el ? el.value : ''); }""", sel) or ""


def location_option_matcher(city: str, country: str | None):
    """Accept an option only if its first part is exactly your city, and it names your country when that is known
    ("Thane, Maharashtra, India" for Thane/India; never "Navi Mumbai" or "Thane, Ontario")."""
    want_city, want_country = _norm(city.split(",")[0]), _norm(country)

    def accept(option: str) -> bool:
        parts = [_norm(x) for x in option.split(",")]
        return bool(parts) and parts[0] == want_city and (not want_country or want_country in parts[1:]
                                                          or len(parts) == 1)
    return accept


# The options of *this* drop-down only: other lists on the page (e.g. a phone field's always-present country-code list)
# must never be read or clicked. Found through aria-controls / aria-owns, else the drop-down's own container.
OWN_OPTIONS_JS = """(s) => {
  const el = document.querySelector(s); if (!el) return [];
  const ids = [el.getAttribute('aria-controls'), el.getAttribute('aria-owns')].filter(Boolean);
  let lists = ids.map(id => document.getElementById(id)).filter(Boolean);
  if (!lists.length) {
    // nearest ancestor holding options, but never one that also holds another drop-down
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const combos = a.querySelectorAll('input[role=combobox], select');
      if (combos.length > 1) break;
      if (a.querySelector('[role=option], [role=listbox]')) { lists = [a]; break; }
    }
  }
  const out = [];
  lists.forEach(l => l.querySelectorAll('[role=option]').forEach(o => {
    const st = window.getComputedStyle(o);
    if (st.display === 'none' || st.visibility === 'hidden' || o.offsetParent === null) return;
    window.__jpOpt = (window.__jpOpt || 0) + 1;  // unique on the page: other lists may keep their old options
    const m = 'jpo-' + window.__jpOpt; o.setAttribute('data-jobpilot-option', m);
    out.push({marker: m, text: (o.innerText || '').trim()});
  }));
  return out;
}"""
_DIAL_CODE = re.compile(r"\s*\+\d{1,4}$")  # "India +91": the country is "India"


def option_equals(option: str, answer: str) -> bool:
    """Exact, ignoring case, spacing and a trailing phone dial code; nothing looser."""
    return _norm(option) == _norm(answer) or _norm(_DIAL_CODE.sub("", option)) == _norm(answer)


def _select_combobox(page, sel: str, answer: str, accept=None, wait_ms: int = 4000) -> bool:
    """Pick the one option of this searchable drop-down that equals ``answer`` (see ``option_equals``) — or, with
    ``accept``, the one option it accepts — and check it is shown as chosen. Options that load from the network are
    waited for (up to ``wait_ms``). None or several: nothing is chosen and the search text is cleared."""
    box = page.locator(sel)
    try:
        box.click(timeout=5000)
        box.fill(answer.split(",")[0].strip() if accept else answer)
        opts: list[dict] = []
        waited = 0
        while waited <= wait_ms:  # the list filters as you type; some (city search) load from the network
            page.wait_for_timeout(250)
            waited += 250
            opts = page.evaluate(OWN_OPTIONS_JS, sel)
            if opts and not any("loading" in _norm(o["text"]) for o in opts) and \
                    any((accept(o["text"]) if accept else option_equals(o["text"], answer)) for o in opts):
                break
        hits = [o for o in opts if (accept(o["text"]) if accept else option_equals(o["text"], answer))]
        if len(hits) != 1:
            box.fill("")
            page.keyboard.press("Escape")
            return False
        page.click(f"[data-jobpilot-option='{hits[0]['marker']}']", timeout=5000)
        page.wait_for_timeout(200)
        shown, chosen = _shown_choice(page, sel).strip(), hits[0]["text"].strip()
        # Some country pickers then show only the dial code of the chosen country ("India +91" → "+91").
        dial_only = bool(re.fullmatch(r"\+\d{1,4}", shown)) and chosen.endswith(shown)
        return _norm(shown) == _norm(chosen) or option_equals(shown, answer) or dial_only
    except Exception:
        return False


def _fill_answer(page, f: dict, sel: str, answer: str) -> bool:
    """Put an approved answer into a text box or an exact-match dropdown option. Anything else: no guessing."""
    if f.get("combobox"):
        return _select_combobox(page, sel, answer)
    if f["tag"] == "select":
        options = page.eval_on_selector(sel, "el => Array.from(el.options).map(o => [o.value, o.text.trim()])")
        match = [v for v, t in options if t.strip().lower() == answer.strip().lower()]
        if len(match) != 1:
            return False
        page.select_option(sel, value=match[0])
        return True
    if f["type"] in TEXT_TYPES or f["tag"] == "textarea":
        page.fill(sel, answer)
        return True
    return False


def _read_back(page, f: dict, sel: str) -> str:
    """The field's actual value in the page now (not what was typed)."""
    if f.get("combobox"):
        return _shown_choice(page, sel)
    if f["type"] == "file":
        return page.evaluate("(s) => { const e = document.querySelector(s); return e && e.files.length ? "
                             "e.files[0].name : ''; }", sel) or ""
    if f["type"] == "contenteditable":
        return page.evaluate("(s) => (document.querySelector(s) || {}).innerText || ''", sel)
    return page.input_value(sel)


def _same_value(actual: str, value: str, key: str | None) -> bool:
    if key == "phone":  # pages format numbers as you type ("98765 43210", "+91 …"): the digits must be yours
        a, v = re.sub(r"\D", "", actual), re.sub(r"\D", "", value)
        return bool(v) and a.endswith(v) and len(a) - len(v) <= 3
    return actual.strip() == value.strip()


def _type_verified(page, f: dict, sel: str, value: str, key: str | None = None) -> tuple[bool, str]:
    """Type ``value`` and confirm the page holds exactly it (phone: the same digits); one retry; a wrong value is
    cleared, never left."""
    for _ in range(2):
        page.fill(sel, value)
        actual = _read_back(page, f, sel)
        if _same_value(actual, value, key):
            return True, actual
    page.fill(sel, "")
    return False, actual


def _report_line(entry: dict) -> str:
    return entry["field"] + (f" — {entry['detail']}" if entry.get("detail") else "")


def assist_page(page, values: dict[str, str], resume_path: Path | None, answer_for=None,
                stop_on_blockers: bool = True, passes: int = 3) -> AssistResult:
    """Fill a loaded application page. Never submits. Testable against any page.

    ``answer_for(label, sensitive) -> str | None``: an approved answer for a question, or None. Used for questions that
    are not plain profile fields; text boxes, exact-match drop-downs and radio groups only.

    ``stop_on_blockers``: with a CAPTCHA / login / MFA on the page, fill nothing (the unattended path). The assisted
    path, where you are at the screen, passes False: the form is filled around the check, which is left untouched for
    you to complete, and it is reported as a pause reason.

    Every value written is read back from the page; fields that appear after an answer are picked up on a re-scan
    (``passes``). ``res.report`` says what was filled and verified, what is still missing, what needs your decision,
    which optional fields stay blank, and what could not be identified or verified.
    """
    res = AssistResult()
    rep = res.report
    res.pause_reasons = detect_blockers(page)
    if res.pause_reasons and stop_on_blockers:
        res.status = "paused"
        res.message = "Paused before filling: " + "; ".join(res.pause_reasons)
        return res

    def done(label, key, source):
        res.filled.append(f"{label} ← {key}")
        rep["filled_verified"].append({"field": label, "detail": source})

    seen: set[str] = set()
    for _ in range(max(1, passes)):
        new = [f for f in page.evaluate(COLLECT_FIELDS_JS) if f["marker"] not in seen and f["type"] != "radio"]
        if not new:
            break
        for f in new:
            seen.add(f["marker"])
            _fill_one(page, f, values, resume_path, answer_for, res, done)
        page.wait_for_timeout(300)  # answers can reveal more fields
    seen_groups: set[str] = set()
    for _ in range(max(1, passes)):
        groups = [g for g in page.evaluate(RADIO_GROUPS_JS) if (g["name"] or g["question"]) not in seen_groups]
        if not groups:
            break
        for g in groups:
            seen_groups.add(g["name"] or g["question"])
            _fill_radio_group(page, g, answer_for, res, done)
        page.wait_for_timeout(300)
    try:  # nothing typed later (a stray key, browser autofill) should land in a field JobPilot filled
        page.evaluate("() => { const a = document.activeElement; if (a && a.blur) a.blur(); }")
    except Exception:
        pass

    res.unknown = list(dict.fromkeys(res.unknown))
    res.sensitive = list(dict.fromkeys(res.sensitive))
    if res.unknown or res.sensitive or res.pause_reasons:
        res.status = "paused"
        if res.unknown:
            res.pause_reasons.append(f"{len(res.unknown)} question(s) need your answers")
        if res.sensitive:
            res.pause_reasons.append(f"{len(res.sensitive)} consent/legal/personal question(s) for you to answer")
    res.message = (f"Filled and verified {len(rep['filled_verified'])} field(s); {len(rep['required_missing'])} "
                   f"required missing; {len(rep['awaiting_answer'])} awaiting your answer; "
                   f"{len(rep['legal_decision'])} legal/consent decision(s) for you; "
                   f"{len(rep['unverified'])} not identified/verified. Review everything and submit yourself.")
    blockers = [r for r in res.pause_reasons if "CAPTCHA" in r or "login" in r or "verification" in r]
    if blockers:
        res.message += " The page also has a check only you can complete (JobPilot did not touch it): " + "; ".join(
            b.split(" — ")[0] for b in blockers) + "."
    return res


def _fill_one(page, f: dict, values: dict[str, str], resume_path: Path | None, answer_for, res: AssistResult, done):
    rep = res.report
    action, key = classify(f)
    label = (f.get("label") or f.get("name") or f.get("id") or f["marker"]).strip()[:200]
    sel = f"[data-jobpilot='{f['marker']}']"
    required = bool(f["required"])
    already = f["checked"] if f["type"] == "checkbox" else bool(f["value"])  # a checkbox's value is "on" either way
    if already and action != "upload":
        return  # never overwrite what's there
    if action == "ambiguous":
        rep["unverified"].append({"field": label, "detail": f"label matches several profile fields ({key}); not filled"})
        if required:
            res.unknown.append(f"{label} (ambiguous label)")
        return
    from jobpilot.services.answers import is_legal

    legal = is_legal(label)
    if answer_for is not None and action in ("unknown", "sensitive") and not legal \
            and f["type"] not in ("checkbox", "radio", "file"):
        answer = answer_for(label, action == "sensitive")
        if answer:
            if f.get("combobox") or f["tag"] == "select":
                ok = _fill_answer(page, f, sel, answer)
                if ok and _norm(_read_back(page, f, sel) if f.get("combobox") else answer) == _norm(answer):
                    return done(label, "approved answer", "approved answer")
                rep["unverified" if ok else "awaiting_answer"].append(
                    {"field": label, "detail": "no option equals your approved answer"})
                res.unknown.append(label)
                return
            ok, actual = _type_verified(page, f, sel, answer)
            if ok:
                return done(label, "approved answer", "approved answer")
            rep["unverified"].append({"field": label, "detail": f"page did not keep the value (showed {actual!r})"})
            res.unknown.append(label)
            return
    if action == "fill":
        if key not in values:
            if required:
                rep["required_missing"].append({"field": label, "detail": f"no confirmed {key.replace('_', ' ')} "
                                                                          "in your profile"})
                res.unknown.append(f"{label} (no confirmed value for {key})")
            else:
                rep["optional_blank"].append({"field": label, "detail": f"no confirmed {key.replace('_', ' ')}"})
            return
        if f["tag"] == "select":
            ok = _fill_answer(page, f, sel, values[key])
            if ok:
                return done(label, key, f"profile: {key}")
            (rep["required_missing"] if required else rep["optional_blank"]).append(
                {"field": label, "detail": f"no option equals your {key.replace('_', ' ')} ({values[key]})"})
            if required:
                res.unknown.append(label)
            return
        if f.get("combobox"):
            accept = location_option_matcher(values[key], values.get("country")) if key == "location" else None
            if _select_combobox(page, sel, values[key], accept=accept):
                shown = _shown_choice(page, sel).strip()
                return done(label, key, f"profile: {key} ({shown})" if _norm(shown).startswith(_norm(values[key]))
                            else f"profile: {key} {values[key]} (shown as {shown})")
            detail = (f"no single option is exactly your city {values[key]!r}" if key == "location"
                      else f"no option equals your {key.replace('_', ' ')} ({values[key]})")
            (rep["required_missing"] if required else rep["optional_blank"]).append({"field": label, "detail": detail})
            if required:
                res.unknown.append(f"{label} ({detail})")
            return
        ok, actual = _type_verified(page, f, sel, values[key], key)
        if ok:
            return done(label, key, f"profile: {key}")
        rep["unverified"].append({"field": label, "detail": f"page did not keep {key} (showed {actual!r}); cleared"})
        res.unknown.append(f"{label} (could not be verified)")
        return
    if action == "upload":
        if f["value"]:
            return
        if resume_path and resume_path.exists():
            page.set_input_files(sel, str(resume_path))
            shown = _read_back(page, f, sel)
            if shown == resume_path.name or not shown:  # some widgets move the file into their own list
                return done(label, "resume file", f"approved resume ({resume_path.name})")
            rep["unverified"].append({"field": label, "detail": f"file input shows {shown!r}"})
            return
        rep["required_missing"].append({"field": label, "detail": "no approved resume file"})
        res.unknown.append(f"{label} (no approved resume file)")
        return
    if action == "sensitive":
        if required:
            rep["legal_decision"].append({"field": label, "detail": "legal / consent / personal: yours to answer or sign"})
            res.sensitive.append(label)
        else:
            rep["optional_blank"].append({"field": label, "detail": "optional personal / legal question; never answered for you"})
        return
    if action == "unknown":
        if f["type"] == "file":
            rep["awaiting_answer"].append({"field": label, "detail": "required upload other than your resume "
                                                                   "(e.g. cover letter): JobPilot never writes one"})
        else:
            rep["awaiting_answer"].append({"field": label, "detail": "no approved answer"})
        res.unknown.append(label)
        return
    if f["type"] == "file":
        rep["optional_blank"].append({"field": label, "detail": "optional upload (e.g. cover letter): not attached"})
    elif not f["value"] and f["type"] not in ("checkbox",):
        rep["optional_blank"].append({"field": label, "detail": "optional"})


def _fill_radio_group(page, g: dict, answer_for, res: AssistResult, done):
    rep = res.report
    if g["checked"]:
        return  # never change what's there
    question = (g["question"] or g["name"] or "radio question").strip()[:200]
    decision, option = _radio_plan(g, answer_for)
    if decision == "choose":
        page.check(f"[data-jobpilot-radio='{option['marker']}']")
        if page.is_checked(f"[data-jobpilot-radio='{option['marker']}']"):
            res.filled.append(f"{question} ← approved answer ({option['label']})")
            rep["filled_verified"].append({"field": question, "detail": f"approved answer ({option['label']})"})
        else:
            rep["unverified"].append({"field": question, "detail": "the option did not stay selected"})
            res.unknown.append(question)
        return
    if not g["required"]:
        rep["optional_blank"].append({"field": question, "detail": "optional"})
        return
    if decision == "sensitive":
        rep["legal_decision"].append({"field": question, "detail": "legal / consent / personal: yours to answer"})
        res.sensitive.append(question)
    else:
        why = "no option equals your approved answer" if decision == "no_exact_option" else "no approved answer"
        rep["awaiting_answer"].append({"field": question, "detail": why})
        res.unknown.append(question + (" (no option equals your approved answer)" if decision == "no_exact_option" else ""))


MISSING_REQUIRED_JS = """
() => {
  const out = [];
  const seenRadio = new Set();
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (!required) continue;
    const lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    const box = !lab && el.closest('[class*="select__container"], [class*="container"]');
    const boxLabel = box && box.querySelector('label');
    const label = (lab && lab.innerText) || (boxLabel && boxLabel.innerText) || el.getAttribute('aria-label') || el.name
      || el.id || type;
    if (type === 'radio') {
      if (seenRadio.has(el.name)) continue;
      seenRadio.add(el.name);
      if (!document.querySelector('input[type=radio][name="' + CSS.escape(el.name) + '"]:checked')) out.push((label || el.name).trim());
    } else if (type === 'checkbox') {
      if (!el.checked) out.push((label || el.name).trim());
    } else if (type === 'file') {
      if (!el.files.length) out.push((label || 'file').trim());
    } else if (el.getAttribute('role') === 'combobox') {  // the chosen option is shown beside the (cleared) input
      const shown = el.closest('[class*="value-container"], [class*="control"]');
      const single = shown && shown.querySelector('[class*="single-value"], [class*="singleValue"]');
      if (!String(el.value || '').trim() && !(single && single.innerText.trim())) out.push((label || el.name).trim());
    } else if (!String(el.value || '').trim()) {
      out.push((label || el.name).trim());
    }
  }
  return out;
}
"""

FIND_SUBMIT_JS = """
() => {
  const els = Array.from(document.querySelectorAll('button, input[type=submit]')).filter(el => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || el.disabled) return false;
    const type = (el.getAttribute('type') || (el.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase();
    const text = (el.innerText || el.value || '').trim().toLowerCase();
    return type === 'submit' && /submit|apply/.test(text) && !/save|cancel|back|draft/.test(text);
  });
  els.forEach((el, i) => el.setAttribute('data-jobpilot-submit', String(i)));
  return els.map(el => (el.innerText || el.value || '').trim());
}
"""

VALIDATION_ERRORS_JS = """
() => {
  const bad = [];
  for (const el of document.querySelectorAll('[aria-invalid="true"], .error, .field-error, .error-message, [role=alert]')) {
    const style = window.getComputedStyle(el);
    const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (style.display !== 'none' && style.visibility !== 'hidden' && (text || el.getAttribute('aria-invalid') === 'true')) {
      bad.push(text.slice(0, 120) || (el.name || el.id || 'invalid field'));
    }
  }
  return bad;
}
"""


def plan_page(page, values: dict[str, str], resume_available: bool, answer_for=None) -> dict[str, Any]:
    """What a submission of this page *would* do, without typing, uploading or clicking anything.

    For each visible field: where its value would come from (a confirmed profile fact, an approved answer, the
    approved resume) or why it can't be filled (needs your answer / personal or legal question / no value). Used to
    validate a platform against real application forms without submitting them.
    """
    blockers = detect_blockers(page)
    fields = page.evaluate(COLLECT_FIELDS_JS)
    plan, missing, sensitive = [], [], []
    for g in page.evaluate(RADIO_GROUPS_JS):
        question = (g["question"] or g["name"] or "radio question")[:120]
        decision, option = _radio_plan(g, answer_for)
        if g["checked"]:
            how = "already answered"
        elif decision == "choose":
            how = f"approved answer ({option['label']})"
        elif not g["required"]:
            how = "left empty (optional)"
        elif decision == "sensitive":
            sensitive.append(question)
            how = "you answer (personal / legal / consent)"
        else:
            missing.append(question)
            how = "MISSING" + (" (no option equals your approved answer)" if decision == "no_exact_option" else "")
        plan.append({"label": question, "type": "radio", "required": bool(g["required"]), "plan": how})
    for f in fields:
        if f["type"] == "radio":
            continue
        action, key = classify(f)
        label = (f.get("label") or f.get("name") or f.get("id") or f["marker"]).strip()[:120]
        source = None
        if answer_for is not None and action in ("unknown", "sensitive") and f["type"] not in ("checkbox", "radio", "file"):
            if answer_for(label, action == "sensitive"):
                exact = f["tag"] == "select" or f.get("combobox")
                source = "approved answer" + (" (must equal one option exactly)" if exact else "")
        if source is None:
            if action == "fill":
                source = (f"profile: {key}" + (" (must equal one option exactly)" if f.get("combobox") else "")
                          if key in values and f["tag"] != "select" else None)
                if source is None and f["required"]:
                    missing.append(f"{label} (no confirmed value for {key})" if key not in values else label)
            elif action == "upload":
                source = "approved resume file" if resume_available else None
                if source is None:
                    missing.append(f"{label} (no approved resume file)")
            elif action == "sensitive":
                if f["required"]:
                    sensitive.append(label)
                    source = "you answer (personal / legal / consent)"
                else:
                    source = "left empty (optional personal / legal question; never answered for you)"
            elif action == "unknown":
                missing.append(label)
        plan.append({"label": label, "type": f["type"], "required": bool(f["required"]),
                     "plan": source or ("left empty (optional)" if not f["required"] else "MISSING")})
    submits = find_submit(page)
    missing, sensitive = list(dict.fromkeys(missing)), list(dict.fromkeys(sensitive))
    ready = not blockers and not missing and not sensitive and len(submits) == 1
    return {"url": page.url, "fields": plan, "missing": missing, "needs_you": sensitive, "blockers": blockers,
            "submit_controls": submits, "would_submit_unattended": ready,
            "verdict": ("ready: every required field has an approved value and there is one submit control" if ready
                        else "not submittable unattended: " + "; ".join(
                            ([f"{len(blockers)} blocker(s)"] if blockers else [])
                            + ([f"{len(missing)} required field(s) without an approved value"] if missing else [])
                            + ([f"{len(sensitive)} question(s) only you can answer"] if sensitive else [])
                            + ([f"{len(submits)} submit controls (need exactly 1)"] if len(submits) != 1 else [])))}


def missing_required(page) -> list[str]:
    """Required fields (including radio groups and files) that are still empty after filling."""
    return list(dict.fromkeys(x for x in page.evaluate(MISSING_REQUIRED_JS) if x))


def find_submit(page) -> list[str]:
    """Visible submit controls whose text says submit/apply. Exactly one is required to proceed."""
    return page.evaluate(FIND_SUBMIT_JS)


def classify_outcome(page, start_url: str, timeout_s: float = 30.0, poll_s: float = 0.5) -> dict[str, Any]:
    """After clicking submit: 'confirmed' (confirmation wording, no form errors), 'failed' (the form shows validation
    errors: nothing was accepted), or 'uncertain' (anything else, including a timeout). A click is not proof."""
    deadline = time.monotonic() + timeout_s
    last_text = ""
    while time.monotonic() < deadline:
        try:
            last_text = page.inner_text("body", timeout=2000)[:20000]
            errors = page.evaluate(VALIDATION_ERRORS_JS)
        except Exception:  # navigating: try again
            time.sleep(poll_s)
            continue
        if CONFIRM_TEXT.search(last_text) and not errors:
            return {"outcome": "confirmed", "url": page.url, "text": last_text[:5000],
                    "detail": f"confirmation wording on {page.url}"}
        if errors:
            return {"outcome": "failed", "url": page.url, "text": last_text[:5000],
                    "detail": "the form reported errors: " + "; ".join(errors[:5])}
        time.sleep(poll_s)
    return {"outcome": "uncertain", "url": page.url if not page.is_closed() else start_url, "text": last_text[:5000],
            "detail": f"no confirmation or error within {timeout_s:.0f} s after submitting"}


def _save_screenshot(settings: Settings, page, name: str) -> str | None:
    try:
        data = page.screenshot(full_page=True)
        return storage.store(settings, "browser", f"{name}.png", data).rel_path
    except Exception:  # pragma: no cover - best effort
        return None


# What each platform needs, as checked on real postings (2026-10-18). Shown when autofill isn't offered.
PLATFORM_NOTES = [
    (re.compile(r"(^|\.)greenhouse\.io$"), "Greenhouse hosted form; boards load an invisible reCAPTCHA, so you "
                                            "complete the check and click Submit"),
    (re.compile(r"(^|\.)lever\.co$"), "Lever form"),
    (re.compile(r"(^|\.)ashbyhq\.com$"), "Ashby form"),
    (re.compile(r"myworkdayjobs\.com$"), "Workday: needs an account login per employer"),
    (re.compile(r"(^|\.)amazon\.(jobs|com)$"), "Amazon: applying needs an Amazon jobs account login"),
    (re.compile(r"careers\.microsoft\.com$"), "Microsoft: applying needs a Microsoft account login"),
    (re.compile(r"(^|\.)google\.com$"), "Google: applying needs a Google sign-in"),
    (re.compile(r"icims\.com$"), "iCIMS: applying needs a candidate account"),
    (re.compile(r"careers\.mastercard\.com$"), "Mastercard careers (Phenom) hands off to Workday: account login"),
]


def platform_note(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    for rx, note in PLATFORM_NOTES:
        if rx.search(host):
            return note
    return f"{host or 'no URL'}: apply on the site yourself"


def answer_resolver(session: Session, app: Application, use_bank: bool):
    """Approved answers only: this application's approved questions (sensitive ones you answered yourself), then the
    approved, non-sensitive answer bank. Returns None when there is no approved answer: nothing is guessed."""
    from jobpilot.models import ApplicationQuestion
    from jobpilot.services import answers as answer_svc
    from jobpilot.services.text_utils import normalize

    own = {normalize(q.question): q.answer for q in session.scalars(
        select(ApplicationQuestion).where(ApplicationQuestion.application_id == app.id,
                                          ApplicationQuestion.status == "approved")) if q.answer}

    def answer_for(label: str, sensitive: bool) -> str | None:
        key = normalize(label)
        if key in own:
            return own[key]
        if sensitive or not use_bank:
            return None
        hit = answer_svc.suggest(session, label, app.job.category)
        return hit["entry"].answer if hit and hit["similarity"] >= 0.8 else None

    return answer_for


def application_host(app: Application) -> str | None:
    url = app.apply_url or app.job.apply_url or app.job.source_url
    return (urlsplit(url or "").hostname or None) if url else None


def run_assist(session: Session, settings: Settings, app: Application, wait_for_user: bool = True,
               timeout_minutes: int = 45, approved_host: str | None = None) -> BrowserRun:
    """Launch a visible browser for one application. Caller holds the 'browser' task lock.

    ``approved_host`` must equal the application page's host: you approve each site explicitly.
    """
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
    host = urlsplit(url).hostname
    if not approved_host or approved_host.lower() != (host or "").lower():
        run.status = "failed"
        run.message = f"Not approved: opening {host} needs your explicit approval for this application"
        return _finish(session, run)
    from jobpilot.services import settings_store

    allowed_sites = settings_store.get(session, "browser")["autofill_sites"]
    if run.site and run.site not in allowed_sites:
        run.site = None  # supported in principle, but autofill turned off for this site in Settings
    audit.record(session, "browser.site_approved", "application", app.id, host=host)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        run.status, run.message = "failed", "Playwright not installed (pip install -e '.[browser]')"
        return _finish(session, run)

    values = profile_values(session)
    from jobpilot.services import resumes as resume_svc

    from jobpilot.services import tailored

    resume_problem = None
    try:  # only the exact approved bytes (or this job's validated tailored resume) are ever attached
        resume_path, _ = tailored.attachment_for(session, settings, app)
    except resume_svc.ResumeFileError as exc:
        resume_path, resume_problem = None, str(exc)

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
            try:  # application forms are rendered by scripts: let them finish before looking for fields
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            if run.site is None:
                run.status = "unsupported"
                run.message = (f"No autofill here ({platform_note(url)}). The page is open for you to "
                               f"complete manually: {url} — then record the submission with evidence in JobPilot. "
                               "Autofill is offered on Greenhouse and Lever hosted forms.")
                res = AssistResult(status="unsupported", pause_reasons=detect_blockers(page))
            else:
                # Approved answers only (this application's, then the answer bank if you allow it in Settings).
                use_bank = bool(settings_store.get(session, "browser").get("use_answer_bank", True))
                # You are at the screen: fill around any CAPTCHA / bot check and leave the check itself to you.
                res = assist_page(page, values, resume_path, answer_for=answer_resolver(session, app, use_bank),
                                  stop_on_blockers=False)
                run.status = res.status
                run.message = res.message
                if not values:
                    run.message += " (profile not confirmed — no personal fields were filled)"
            if resume_problem:
                res.pause_reasons.append(f"resume not attached: {resume_problem}")
                run.message = (run.message or "") + f" Resume NOT attached: {resume_problem}."
            run.filled_fields = res.filled  # only values read back from the page
            labels = {"required_missing": "[required, missing]", "awaiting_answer": "[awaiting your answer]",
                      "legal_decision": "[legal/consent: yours]", "optional_blank": "[optional, left blank]",
                      "unverified": "[not identified/verified]"}
            run.unknown_fields = [f"{labels[k]} {_report_line(e)}" for k in labels for e in res.report[k]] or \
                (res.unknown + [f"[you answer] {s}" for s in res.sensitive])
            run.report = res.report  # transient (not stored as a column): printed by `jobpilot assist`
            run.pause_reasons = res.pause_reasons
            from jobpilot.services import answers as answer_svc

            # questions the profile has now answered on the form are closed; new ones are recorded for you
            answer_svc.resolve_from_form(session, app, [e["field"] for e in res.report["filled_verified"]])
            if res.unknown or res.sensitive:
                answer_svc.add_questions(session, app, res.unknown + res.sensitive, source="browser")
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
