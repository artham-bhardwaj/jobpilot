"""Milestone 13: application shell and pages after the light-theme redesign (synthetic data only)."""
from __future__ import annotations

import re

import pytest

NAV = ["/", "/jobs", "/matching", "/review", "/applications", "/answers", "/interviews", "/hub", "/profile", "/resumes",
       "/skills", "/contacts", "/companies", "/drafts", "/analytics", "/sources", "/settings", "/system"]


@pytest.mark.parametrize("path", NAV + ["/gmail", "/jobs/new", "/drafts/templates", "/contacts/duplicates"])
def test_every_page_renders_in_the_shared_shell(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    html = r.text
    assert 'class="skip-link" href="#main"' in html and '<main class="content" id="main"' in html
    assert len(re.findall(r"<h1[ >]", html)) == 1, f"{path}: exactly one page title"
    for href in NAV:
        assert f'href="{href}"' in html, f"{path}: nav link {href} kept"
    current = re.findall(r'<a href="([^"]+)" class="active" aria-current="page">', html)
    assert len(current) == 1, f"{path}: one active nav item"
    expected = {"/gmail": "/settings", "/jobs/new": "/jobs", "/drafts/templates": "/drafts",
                "/contacts/duplicates": "/contacts"}.get(path, path)
    assert current[0] == expected


def test_stylesheet_is_dark_by_default_with_a_light_option_and_token_based(client):
    css = client.get("/static/app.css").text
    assert "prefers-color-scheme" not in css, "the theme is the user's choice, not the OS setting"
    assert ":focus-visible" in css and "prefers-reduced-motion" in css
    assert "color-scheme: dark" in css.split(":root[data-theme=\"light\"]")[0], "dark (Midnight) is the default"
    rules = css.split(":root[data-theme=\"light\"] {")[1].split("}", 1)[1]  # everything after the token blocks
    hexes = set(re.findall(r"#[0-9a-fA-F]{3,6}\b", rules))
    assert len(hexes) <= 3, f"colours come from tokens, found literals: {sorted(hexes)}"


def test_overview_shows_only_real_counts_including_outreach(client, db):
    from jobpilot.services import contacts as cs
    from jobpilot.services import drafts as ds
    from tests.helpers import seed_profile

    seed_profile(db)
    c, _ = cs.save_contact(db, cs.ContactIn(name="Jane Recruiter", email="jane@example.com"))
    d = ds.create_draft(db, ds.DraftCreate(template_key="recruiter_outreach", contact_id=c.id))
    ds.edit_draft(db, d, ds.DraftEdit(to_address="jane@example.com", subject="Hello", body="Hi (synthetic)."))
    ds.approve(db, d)
    db.commit()
    html = client.get("/").text
    assert "Outreach" in html and "Approved, not sent" in html
    assert re.search(r"Approved, not sent</a><b>1</b>", html)
    assert re.search(r"in your Gmail Drafts.*?<b>0</b>", html, re.S), "nothing claimed in Gmail without Gmail's confirmation"


def test_navigation_is_grouped_into_sections_with_the_current_one_open(client):
    html = client.get("/desk").text
    sections = re.findall(r'<details class="nav-section"( open)?>\s*<summary class="nav-group[^"]*">.*?<span>([^<]+)</span>', html, re.S)
    assert [name for _, name in sections] == ["Home", "Find jobs", "Apply", "Outreach", "You", "Settings"]
    assert [name for is_open, name in sections if is_open] == ["Apply"], "only the current section is expanded"
    assert '<span class="topbar-crumb">' in html and "<b>Job desk</b>" in html
    assert "data-theme-toggle" in html and '<script src="/static/theme.js"></script>' in html
    assert "localStorage" in client.get("/static/theme.js").text
