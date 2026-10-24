"""Milestone 22: real-form behaviour found on live Greenhouse/Lever forms, reproduced with synthetic pages on
127.0.0.1 (searchable dropdowns, invisible bot checks, optional uploads), the read-only form plan and the Telegram
/outcomes command. No employer is contacted."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from jobpilot.models import Application, ApplicationEvent, BrowserRun, Job, utcnow
from tests.helpers import LocalPage, make_http
from tests.apply_fixtures import job, world  # noqa: F401 - fixture

# A searchable dropdown built like react-select (what Greenhouse's job boards use): a role=combobox input labelled
# through aria-labelledby, the chosen option shown beside it, and a nameless unfocusable "required" proxy input.
COMBO_JS = """
<script>
document.querySelectorAll('.select__container').forEach(c => {
  const input = c.querySelector('input[role=combobox]'), menu = c.querySelector('[role=listbox]');
  const single = c.querySelector('.select__single-value'), proxy = c.querySelector('.requiredInput');
  const opts = JSON.parse(c.dataset.options);
  const render = () => { menu.innerHTML = ''; menu.style.display = 'block';
    opts.filter(o => o.toLowerCase().includes(input.value.toLowerCase())).forEach(o => {
      const d = document.createElement('div'); d.setAttribute('role', 'option'); d.innerText = o;
      d.onclick = () => { single.innerText = o; single.style.display = 'block'; proxy.value = o; input.value = '';
                          menu.style.display = 'none'; };
      menu.appendChild(d); }); };
  input.addEventListener('focus', render); input.addEventListener('input', render);
});
</script>"""


def combo(qid: str, label: str, options: list[str], required: bool = True) -> str:
    return (f"<div class='select__container' data-options='{json.dumps(options)}'>"
            f"<label id='{qid}-label' for='{qid}'>{label}{'*' if required else ''}</label>"
            f"<div class='select__control'><div class='select__value-container'>"
            f"<div class='select__single-value' style='display:none'></div>"
            f"<input id='{qid}' role='combobox' type='text' aria-labelledby='{qid}-label' "
            f"aria-required='{'true' if required else 'false'}'></div></div>"
            f"<input tabindex='-1' class='requiredInput' {'required' if required else ''} value='' "
            f"style='opacity:0;height:1px;width:1px'>"
            f"<div role='listbox' style='display:none'></div></div>")


def page_html(*parts: str, head: str = "") -> str:
    return (f"<html><head>{head}</head><body><form>"
            "<label for=fn>First Name*</label><input id=fn name=first_name required>"
            "<label for=em>Email*</label><input id=em name=email type=email required>"
            "<label for=cv>Resume/CV</label><input id=cv name=resume type=file>"
            "<label for=cl>Cover Letter</label><input id=cl name=cover_letter type=file>"
            + "".join(parts) + "<button type=submit>Submit application</button></form>" + COMBO_JS + "</body></html>")


@pytest.fixture
def browser_page():
    sync = pytest.importorskip("playwright.sync_api")
    from jobpilot.config import get_settings

    pages: list[LocalPage] = []
    with sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel=get_settings().browser_channel or None, headless=True)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"no browser: {exc}")

        def open_page(html: str):
            site = LocalPage(html)
            pages.append(site)
            p = b.new_page()
            p.goto(site.url, wait_until="domcontentloaded")
            return p, site

        yield open_page
        b.close()
    for s in pages:
        s.close()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
VALUES = {"first_name": "Test", "email": "test.candidate@example.com"}


def test_searchable_dropdown_gets_the_exact_option_and_proxy_is_not_a_field(browser_page):
    from jobpilot.automation.apply import COLLECT_FIELDS_JS, assist_page, missing_required

    page, site = browser_page(page_html(combo("q1", "Are you located in India?", ["Yes", "No", "Yes, in Pune"])))
    fields = page.evaluate(COLLECT_FIELDS_JS)
    labels = [f["label"] for f in fields]
    assert "Are you located in India?*" in labels and not any(f["type"] == "text" and not f["label"] for f in fields)
    res = assist_page(page, VALUES, None, answer_for=lambda label, sensitive: "Yes" if "India" in label else None)
    assert "Are you located in India?* ← approved answer" in res.filled
    assert page.inner_text(".select__single-value") == "Yes", "the exact option, not 'Yes, in Pune'"
    assert missing_required(page) == [] and not any(r.startswith("POST") for r in site.requests)


def test_dropdown_without_an_exact_option_is_left_for_you(browser_page):
    from jobpilot.automation.apply import assist_page, missing_required

    page, _ = browser_page(page_html(combo("q1", "Are you located in India?", ["Yes", "No"])))
    res = assist_page(page, VALUES, None, answer_for=lambda label, sensitive: "Maybe")
    assert "Are you located in India?*" in res.unknown and res.status == "paused"
    assert page.inner_text(".select__single-value") == "" and page.input_value("#q1") == ""
    assert missing_required(page) == ["Are you located in India?*"], "the proxy keeps the field flagged as missing"


def test_optional_cover_letter_and_optional_personal_questions_do_not_block(browser_page):
    from jobpilot.automation.apply import plan_page

    page, _ = browser_page(page_html(combo("g", "Gender", ["Male", "Female", "Decline"], required=False)))
    plan = plan_page(page, VALUES, resume_available=True)
    by_label = {f["label"]: f["plan"] for f in plan["fields"]}
    assert by_label["Cover Letter"] == "left empty (optional)" and by_label["Resume/CV"] == "approved resume file"
    assert by_label["Gender"].startswith("left empty (optional personal")
    assert plan["missing"] == [] and plan["needs_you"] == [] and plan["would_submit_unattended"]


def test_plan_page_types_uploads_and_clicks_nothing(browser_page):
    from jobpilot.automation.apply import plan_page

    page, site = browser_page(page_html(combo("q1", "Will you require visa sponsorship?", ["Yes", "No"]),
                                        combo("q2", "Have you worked here before?", ["Yes", "No"])))
    plan = plan_page(page, VALUES, resume_available=False,
                     answer_for=lambda label, sensitive: None if sensitive else ("No" if "worked" in label else None))
    by_label = {f["label"]: f["plan"] for f in plan["fields"]}
    assert by_label["First Name*"] == "profile: first_name"
    assert by_label["Have you worked here before?*"].startswith("approved answer (must equal one option")
    assert plan["needs_you"] == ["Will you require visa sponsorship?*"] and not plan["would_submit_unattended"]
    assert page.input_value("#fn") == "" and page.input_value("#em") == "" and page.inner_text("body").count("No") == 0
    assert not any(r.startswith("POST") for r in site.requests)


@pytest.mark.parametrize("head,invisible", [
    ("<script async src='https://www.recaptcha.net/recaptcha/enterprise.js?render=6LfTESTKEY'></script>", True),
    ("<script async src='https://www.google.com/recaptcha/api.js'></script><div class='g-recaptcha' "
     "data-sitekey='x'></div>", False),
    ("<script async src='https://js.hcaptcha.com/1/api.js'></script>", False),
])
def test_bot_checks_are_named_and_always_block(browser_page, head, invisible):
    from jobpilot.automation.apply import CAPTCHA_REASON, INVISIBLE_REASON, plan_page

    page, _ = browser_page(page_html(head=head))
    plan = plan_page(page, VALUES, resume_available=True)
    assert plan["blockers"] == [INVISIBLE_REASON if invisible else CAPTCHA_REASON]
    assert not plan["would_submit_unattended"]


def test_classify_upload_rules():
    from jobpilot.automation.apply import classify

    assert classify({"type": "file", "label": "Resume/CV", "name": "", "id": ""}) == ("upload", "resume")
    assert classify({"type": "file", "label": "Cover Letter", "name": "", "id": "", "required": False}) == ("skip", None)
    assert classify({"type": "file", "label": "Portfolio PDF", "name": "", "id": "", "required": True}) == ("unknown", None)


# ---- /outcomes ------------------------------------------------------------------

def test_outcomes_command_lists_verified_results(world, db, settings):  # noqa: F811
    from jobpilot.services import applications as apps
    from jobpilot.services.telegram_bot import cmd_outcomes

    assert "No submission attempts" in cmd_outcomes(db, settings)
    j = job(db, "https://job-boards.greenhouse.io/examplecorp/jobs/9")
    app = apps.create(db, j)
    db.add_all([BrowserRun(application_id=app.id, status="uncertain", finished_at=utcnow()),
                BrowserRun(application_id=app.id, status="running")])  # an attempt in progress is not an outcome
    db.commit()
    text = cmd_outcomes(db, settings)
    assert "uncertain — never resubmitted" in text and text.count("Java Backend Engineer at Examplecorp") == 1
    assert "→ preparing" in text or db.scalar(select(func.count(ApplicationEvent.id))) == 0


# ---- whole-board sources: filters and Greenhouse's own form ----------------------------------------------------

def test_greenhouse_filters_light_listing_then_reads_matches(settings):
    from jobpilot.sources.ats import GreenhouseAdapter

    base = f"{GreenhouseAdapter.BASE}/examplecorp/jobs"
    listing = {"jobs": [{"id": 1, "title": "Software Engineer", "location": {"name": "Bengaluru, India"}},
                        {"id": 2, "title": "Senior Software Engineer", "location": {"name": "Bengaluru, India"}},
                        {"id": 3, "title": "Software Engineer", "location": {"name": "London"}},
                        {"id": 4, "title": "Recruiter", "location": {"name": "Pune, India"}}]}
    detail = {"id": 1, "title": "Software Engineer", "location": {"name": "Bengaluru, India"},
              "content": "&lt;p&gt;Java&lt;/p&gt;", "absolute_url": "https://www.examplecorp.com/careers?gh_jid=1"}
    http = make_http(settings, {base: (200, json.dumps(listing)), f"{base}/1": (200, json.dumps(detail))})
    res = GreenhouseAdapter().fetch(http, {"board_token": "examplecorp", "title_keywords": ["software engineer"],
                                           "location_keywords": ["india"], "exclude_keywords": ["senior"]}, None)
    (j,) = res.items
    assert j.external_id == "1" and j.description_text == "Java" and res.full_snapshot
    assert j.source_url == "https://www.examplecorp.com/careers?gh_jid=1"
    assert j.apply_url == "https://job-boards.greenhouse.io/embed/job_app?for=examplecorp&token=1"
    assert http.session.calls.count(f"{base}/2") == 0 and res.counts == {"listed": 4, "matched": 1, "read": 1}
    from jobpilot.automation.apply import site_for

    assert site_for(j.apply_url) == "greenhouse"


def test_hosted_greenhouse_url_is_kept_and_filters_are_optional(settings):
    from jobpilot.sources.ats import GreenhouseAdapter, LeverAdapter

    gh = GreenhouseAdapter()
    assert gh.form_url("x", 5, "https://job-boards.greenhouse.io/x/jobs/5") == "https://job-boards.greenhouse.io/x/jobs/5"
    assert gh.validate_config({"board_token": "x"}) == {"board_token": "x"}, "existing sources are unchanged"
    postings = [{"id": "a" * 10, "text": "Backend Engineer", "categories": {"location": "Pune, India"}},
                {"id": "b" * 10, "text": "Designer", "categories": {"location": "Pune, India"}}]
    http = make_http(settings, {f"{LeverAdapter.BASE}/examplecorp": (200, json.dumps(postings))})
    res = LeverAdapter().fetch(http, {"site": "examplecorp", "title_keywords": ["engineer"]}, None)
    assert [p.title for p in res.items] == ["Backend Engineer"] and res.full_snapshot


def test_job_ids_are_untouched(db):
    assert db.scalar(select(func.count(Job.id))) == 0  # sanity: the test database starts empty
