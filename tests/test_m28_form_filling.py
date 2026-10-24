"""Milestone 28: accurate assisted form filling with read-back verification and a completion report.

Synthetic pages on 127.0.0.1 modelled on the live Rubrik Greenhouse form (2026-10-18); nothing is submitted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import LocalPage

# A searchable drop-down like Greenhouse's (react-select): options can load after a delay (the city field asks a
# geocoder), the choice is shown beside the cleared input, and a hidden proxy carries `required`.
DROPDOWN_JS = """
<script>
document.querySelectorAll('.select__container').forEach(c => {
  const input = c.querySelector('input[role=combobox]'), menu = c.querySelector('[role=listbox]');
  const single = c.querySelector('.select__single-value'), proxy = c.querySelector('.requiredInput');
  const opts = JSON.parse(c.dataset.options), delay = parseInt(c.dataset.delay || '0');
  let timer = null;
  const render = () => { clearTimeout(timer); menu.innerHTML = '<div role=option>Loading…</div>'; menu.style.display = 'block';
    timer = setTimeout(() => { menu.innerHTML = '';
      opts.filter(o => o.toLowerCase().includes(input.value.toLowerCase())).forEach(o => {
        const d = document.createElement('div'); d.setAttribute('role', 'option'); d.innerText = o;
        d.onclick = () => { single.innerText = o; single.style.display = 'block'; proxy.value = o; input.value = '';
                            menu.style.display = 'none'; c.dispatchEvent(new Event('chosen', {bubbles: true})); };
        menu.appendChild(d); }); }, delay); };
  input.addEventListener('focus', render); input.addEventListener('input', render);
});
</script>"""


def dropdown(qid, label, options, required=True, delay=0):
    return (f"<div class='select__container' data-options='{json.dumps(options)}' data-delay='{delay}'>"
            f"<label id='{qid}-label' for='{qid}'>{label}{'*' if required else ''}</label>"
            f"<div class='select__control'><div class='select__value-container'>"
            f"<div class='select__single-value' style='display:none'></div>"
            f"<input id='{qid}' role='combobox' type='text' aria-labelledby='{qid}-label' "
            f"aria-required='{'true' if required else 'false'}'></div></div>"
            f"<input tabindex='-1' class='requiredInput' {'required' if required else ''} value='' "
            f"style='opacity:0;height:1px;width:1px'><div role='listbox' style='display:none'></div></div>")


def form(*parts, script=""):
    return ("<html><body><form id=f>" + "".join(parts) + "<button type=submit>Submit application</button></form>"
            "<script>document.getElementById('f').addEventListener('submit', e => { e.preventDefault(); "
            "document.body.dataset.submitted = '1'; });</script>" + DROPDOWN_JS + script + "</body></html>")


def text(fid, label, required=True, typ="text"):
    return f"<label for={fid}>{label}{'*' if required else ''}</label><input id={fid} type={typ} {'required' if required else ''}>"


RUBRIK_LIKE = form(
    text("first_name", "First Name"), text("last_name", "Last Name"), text("email", "Email"),
    dropdown("country", "Country", ["India", "Indonesia", "British Indian Ocean Territory"]),
    text("phone", "Phone", typ="tel"),
    dropdown("candidate-location", "Location (City)", ["Thane, Maharashtra, India", "Thane, Ontario, Canada",
                                                       "Navi Mumbai, Maharashtra, India"], delay=900),
    "<label for=resume>Resume/CV*</label><input id=resume type=file required>",
    "<label for=cover_letter>Cover Letter</label><input id=cover_letter type=file>",
    text("linkedin", "LinkedIn Profile", required=False), text("website", "Website", required=False),
    "<label><input type=checkbox name=privacy required> I acknowledge that I have read the Candidate Privacy Notice*</label>",
    text("nda", "Please Review the NDA and indicate your agreement by typing your full name below"),
    text("employer", "What is the name of your current employer?"),
    dropdown("rubrik_before", "Are you currently or have you ever been employed by Examplecorp?", ["Yes", "No"]),
)

VALUES = {"first_name": "Test", "last_name": "Candidate", "email": "test.candidate@example.com", "phone": "9000000000",
          "location": "Thane", "country": "India", "current_company": "Synthetic Consultancy Services",
          "linkedin": "https://www.linkedin.com/in/test-candidate", "website": "https://example.org/"}


@pytest.fixture
def open_page(tmp_path):
    sync = pytest.importorskip("playwright.sync_api")
    from jobpilot.config import get_settings

    sites = []
    with sync.sync_playwright() as pw:
        b = pw.chromium.launch(channel=get_settings().browser_channel or None, headless=True)

        def go(html):
            site = LocalPage(html)
            sites.append(site)
            page = b.new_page()
            page.goto(site.url, wait_until="domcontentloaded")
            return page, site

        yield go
        b.close()
    for s in sites:
        s.close()


@pytest.fixture
def resume(tmp_path) -> Path:
    p = tmp_path / "Candidate_Resume.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic resume")
    return p


def fields(report, section):
    return {e["field"]: e["detail"] for e in report[section]}


def test_rubrik_like_form_fills_every_factual_field_and_reports_the_rest(open_page, resume):
    from jobpilot.automation.apply import assist_page

    page, site = open_page(RUBRIK_LIKE)
    res = assist_page(page, VALUES, resume, stop_on_blockers=False)
    ok = fields(res.report, "filled_verified")
    for label in ("First Name*", "Last Name*", "Email*", "Phone*", "Resume/CV*", "LinkedIn Profile", "Website",
                  "What is the name of your current employer?*"):
        assert label in ok, label
    assert ok["Country*"] == "profile: country (India)"
    assert ok["Location (City)*"] == "profile: location (Thane, Maharashtra, India)", "your city, not Navi Mumbai"
    assert page.input_value("#employer") == "Synthetic Consultancy Services"
    shown = page.eval_on_selector("#country", "e => e.closest('.select__control').querySelector('.select__single-value').innerText")
    assert shown == "India", "the exact option, not Indonesia or British Indian Ocean Territory"
    legal = fields(res.report, "legal_decision")
    assert "I acknowledge that I have read the Candidate Privacy Notice*" in legal
    assert "Please Review the NDA and indicate your agreement by typing your full name below*" in legal
    assert "Are you currently or have you ever been employed by Examplecorp?*" in fields(res.report, "awaiting_answer")
    assert not set(legal) & set(fields(res.report, "awaiting_answer")), "a field is in exactly one section"
    assert page.input_value("#nda") == "", "the NDA signature is never typed for you"
    assert not page.is_checked("input[name=privacy]"), "consent is never ticked"
    assert "Cover Letter" in fields(res.report, "optional_blank"), "optional cover letter: reported, not attached"
    assert res.report["required_missing"] == [] and res.report["unverified"] == []
    assert page.evaluate("document.activeElement === document.body"), "focus left the form fields"
    assert page.evaluate("document.body.dataset.submitted") is None and not any(r.startswith("POST") for r in site.requests)


def test_city_must_be_exactly_your_city(open_page):
    from jobpilot.automation.apply import assist_page

    two = form(dropdown("city", "Location (City)", ["Thane, Maharashtra, India", "Thane, Maharashtra, India "], delay=200))
    page, _ = open_page(two)
    res = assist_page(page, {"location": "Thane", "country": "India"}, None)
    assert "Location (City)*" in fields(res.report, "required_missing"), "two equal options: nothing chosen"
    other = form(dropdown("city", "Location (City)", ["Navi Mumbai, Maharashtra, India", "Thane, Ontario, Canada"]))
    page, _ = open_page(other)
    res = assist_page(page, {"location": "Thane", "country": "India"}, None)
    assert "no single option is exactly your city 'Thane'" in fields(res.report, "required_missing")["Location (City)*"]
    assert page.inner_text(".select__single-value") == ""


def test_country_is_only_filled_when_the_profile_states_it(open_page):
    from jobpilot.automation.apply import assist_page, profile_country

    assert profile_country("Thane") == "India" and profile_country("Thane, Maharashtra") == "India"
    assert profile_country("Remote") is None and profile_country("Springfield") is None
    page, _ = open_page(form(dropdown("country", "Country", ["India", "Indonesia"])))
    res = assist_page(page, {"location": "Remote"}, None)
    assert fields(res.report, "required_missing")["Country*"] == "no confirmed country in your profile"


def test_a_value_the_page_changes_is_cleared_and_reported(open_page):
    """What happened on the live form: the employer field ended up 'Tata Consultancy ServicesS'. Whatever changes a
    value after it is typed, JobPilot now reads it back, retries once, and clears it rather than leave it wrong."""
    from jobpilot.automation.apply import assist_page

    corrupting = form(text("employer", "What is the name of your current employer?"),
                      script="<script>document.getElementById('employer').addEventListener('input', e => {"
                             " const v = e.target.value; if (v && !v.endsWith('S')) e.target.value = v + 'S'; });</script>")
    page, _ = open_page(corrupting)
    res = assist_page(page, {"current_company": "Synthetic Consultancy Services"}, None)
    bad = fields(res.report, "unverified")["What is the name of your current employer?*"]
    assert "Synthetic Consultancy ServicesS" in bad and "cleared" in bad
    assert page.input_value("#employer") == "" and not res.report["filled_verified"]


def test_employer_comes_from_the_confirmed_current_role_and_malformed_values_are_never_typed(db):
    from datetime import date

    from jobpilot.automation.apply import profile_values, validate_values
    from jobpilot.services import profile as ps
    from tests.helpers import seed_profile

    seed_profile(db, skills=("java",), learning=())
    ps.save_fact(db, "employment", ps.EmploymentIn(company="Old Corp", title="Engineer", employment_type="full_time",
                                                    start_date=date(2020, 1, 1), end_date=date(2023, 1, 1),
                                                    status="confirmed"))
    db.flush()
    vals = profile_values(db)
    assert vals["current_company"] == "Synthetic Corp", "the confirmed role without an end date"
    clean, problems = validate_values({"current_company": "Synthetic CorpS", "email": "not-an-email",
                                       "website": "example.org", "phone": "9000000000"}, employers={"Synthetic Corp"})
    assert clean == {"phone": "9000000000"} and len(problems) == 3


def test_ambiguous_labels_are_not_guessed(open_page):
    from jobpilot.automation.apply import assist_page, classify

    assert classify({"type": "text", "label": "GitHub or portfolio URL", "name": "", "id": ""})[0] == "ambiguous"
    assert classify({"type": "text", "label": "First Name*", "name": "", "id": ""}) == ("fill", "first_name")
    page, _ = open_page(form(text("links", "GitHub or portfolio URL")))
    res = assist_page(page, {"github": "https://github.com/x", "website": "https://example.org/"}, None)
    assert "label matches several profile fields" in fields(res.report, "unverified")["GitHub or portfolio URL*"]
    assert page.input_value("#links") == ""


def test_fields_rendered_after_an_answer_are_filled_too(open_page):
    from jobpilot.automation.apply import assist_page

    reveal = form(dropdown("located", "Are you located in India?", ["Yes", "No"]),
                  "<div id=more></div>",
                  script="<script>document.querySelector('.select__container').addEventListener('chosen', () => {"
                         " document.getElementById('more').innerHTML = '<label for=city2>Current location*</label>"
                         "<input id=city2 required>'; });</script>")
    page, _ = open_page(reveal)
    res = assist_page(page, {"location": "Thane"}, None,
                      answer_for=lambda label, sensitive: "Yes" if "located in India" in label else None)
    ok = fields(res.report, "filled_verified")
    assert "Are you located in India?*" in ok and "Current location*" in ok
    assert page.input_value("#city2") == "Thane"


def test_contenteditable_and_native_select(open_page):
    from jobpilot.automation.apply import assist_page

    page, _ = open_page(form(
        "<label id=why-l>Why do you want to work here?*</label><div id=why contenteditable=true aria-labelledby=why-l "
        "aria-required=true style='min-height:20px;border:1px solid'></div>",
        "<label for=c>Country*</label><select id=c required><option value=''>Select</option><option>India</option>"
        "<option>Indonesia</option></select>"))
    res = assist_page(page, {"country": "India"}, None,
                      answer_for=lambda label, sensitive: "I like backend work (synthetic)." if "Why" in label else None)
    ok = fields(res.report, "filled_verified")
    assert "Why do you want to work here?*" in ok and page.inner_text("#why") == "I like backend work (synthetic)."
    assert "Country*" in ok and page.eval_on_selector("#c", "e => e.options[e.selectedIndex].text") == "India"


def test_required_cover_letter_is_your_decision(open_page):
    from jobpilot.automation.apply import assist_page

    page, _ = open_page(form("<label for=cl>Cover Letter*</label><input id=cl type=file required>"))
    res = assist_page(page, {}, None)
    assert "JobPilot never writes one" in fields(res.report, "awaiting_answer")["Cover Letter*"]


def test_run_assist_stores_the_report(world, db, chrome):  # noqa: F811
    from jobpilot.automation.apply import run_assist
    from jobpilot.services import applications as apps
    from tests.apply_fixtures import job

    world.browser_headless = True
    site = LocalPage(RUBRIK_LIKE)
    try:
        app_ = apps.create(db, job(db, site.url, title="Software Engineer"))
        db.commit()
        run = run_assist(db, world, app_, wait_for_user=False, approved_host="127.0.0.1")
        assert "First Name* ← first_name" in run.filled_fields and "Resume/CV* ← resume file" in run.filled_fields
        assert any(u.startswith("[legal/consent: yours] Please Review the NDA") for u in run.unknown_fields)
        assert any(u.startswith("[awaiting your answer] Are you currently or have you ever been employed")
                   for u in run.unknown_fields)
        assert any(u.startswith("[optional, left blank] Cover Letter") for u in run.unknown_fields)
        assert run.report["filled_verified"] and "Filled and verified" in run.message
        assert not any(r.startswith("POST") for r in site.requests)
    finally:
        site.close()


from tests.apply_fixtures import chrome, world  # noqa: E402,F401


# ---- found on the live Rubrik form after the first fix ---------------------------------------------------------

PHONE_COUNTRY_LIST = ("<ul role=listbox id=iti-0__country-listbox>"
                      + "".join(f"<li role=option class=iti__country><span>{c}</span><span>{code}</span></li>"
                                for c, code in (("Afghanistan", "+93"), ("India", "+91"), ("Thane Islands", "+999")))
                      + "</ul>")


def test_only_the_dropdowns_own_options_are_read(open_page):
    """The phone field keeps a country-code list (role=option) in the page; the city search must not read it."""
    from jobpilot.automation.apply import assist_page

    page, _ = open_page(form(PHONE_COUNTRY_LIST,
                             dropdown("city", "Location (City)", ["Thane, Maharashtra, India"], delay=600)))
    res = assist_page(page, {"location": "Thane", "country": "India"}, None)
    assert fields(res.report, "filled_verified")["Location (City)*"] == "profile: location (Thane, Maharashtra, India)"


def test_country_options_with_dial_codes(open_page):
    from jobpilot.automation.apply import assist_page, option_equals

    assert option_equals("India +91", "India") and option_equals("India+91", "India")
    assert not option_equals("British Indian Ocean Territory +246", "India") and not option_equals("Indiana", "India")
    page, _ = open_page(form(dropdown("country", "Country", ["British Indian Ocean Territory +246", "India +91",
                                                             "Indonesia +62"])))
    res = assist_page(page, {"country": "India"}, None)
    assert fields(res.report, "filled_verified")["Country*"] == "profile: country (India +91)"


def test_phone_formatting_by_the_page_is_accepted_but_other_digits_are_not():
    from jobpilot.automation.apply import _same_value

    assert _same_value("98765 43210", "9876543210", "phone") and _same_value("+91 98765 43210", "9876543210", "phone")
    assert not _same_value("98765 43211", "9876543210", "phone") and not _same_value("", "9876543210", "phone")
    assert not _same_value("Tata Consultancy ServicesS", "Tata Consultancy Services", "current_company")


def test_country_picker_that_shows_only_the_dial_code(open_page):
    from jobpilot.automation.apply import assist_page

    page, _ = open_page(form(dropdown("country", "Country", ["British Indian Ocean Territory +246", "India +91"]),
                             script="<script>document.querySelector('.select__container').addEventListener('chosen', e => {"
                                    " const s = e.currentTarget.querySelector('.select__single-value');"
                                    " s.innerText = s.innerText.split(' ').pop(); });</script>"))
    res = assist_page(page, {"country": "India"}, None)
    assert fields(res.report, "filled_verified")["Country*"] == "profile: country India (shown as +91)"
    assert page.inner_text(".select__single-value") == "+91"
