"""Milestone 20: job descriptions that are HTML, double-encoded HTML, or plain text (synthetic postings)."""
from __future__ import annotations

import json

import pytest

from jobpilot.services.text_utils import description_to_text
from jobpilot.sources.pages import extract_from_html, posting_to_job


@pytest.mark.parametrize("value,expected", [
    # normally encoded HTML: converted once, entities decoded, list items kept as lines
    ("<p>Build <b>Java</b> services &amp; APIs</p><ul><li>REST</li><li>SQL</li></ul>",
     "Build Java services & APIs\n\n• REST\n• SQL"),
    ("Line one<br>Line two", "Line one\nLine two"),
    ("<p>Generics: &lt;T&gt; and List&lt;String&gt;</p>", "Generics: <T> and List<String>"),
    # double-encoded HTML: decoded a second time
    ("&lt;p&gt;Build &lt;b&gt;Java&lt;/b&gt; services &amp;amp; APIs&lt;/p&gt;&lt;ul&gt;&lt;li&gt;REST&lt;/li&gt;&lt;/ul&gt;",
     "Build Java services & APIs\n\n• REST"),
    ("&lt;p&gt;Use List&amp;lt;String&amp;gt;&lt;/p&gt;", "Use List<String>"),
    # plain text: no HTML parser, so nothing is swallowed
    ("Plain text\n\n\nwith   extra spaces", "Plain text\n\nwith extra spaces"),
    ("Experience with List<String> and a < b in C++ & Go", "Experience with List<String> and a < b in C++ & Go"),
    ("Use <div> elements wisely; 3 < 5 > 2", "Use <div> elements wisely; 3 < 5 > 2"),
    ("Café — naïve résumé, 2–3 yrs; see https://jobs.example.com/a?b=1&c=2#x",
     "Café — naïve résumé, 2–3 yrs; see https://jobs.example.com/a?b=1&c=2#x"),
    ("Tom &amp; Jerry", "Tom & Jerry"),
    ("", ""),
])
def test_description_to_text(value, expected):
    assert description_to_text(value) == expected


def test_ordinary_text_is_not_decoded_twice():
    # "&amp;lt;" in plain text is an encoded "&lt;": decoded once to the literal characters, not to "<"
    assert description_to_text("Write &amp;lt; as an entity") == "Write &lt; as an entity"


def test_plain_text_line_structure_is_kept():
    text = "Requirements:\n- Java\n- SQL\n\nBenefits:\n- Remote"
    assert description_to_text(text) == text


def _page(description: str) -> str:
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Software Engineer I",
               "hiringOrganization": {"@type": "Organization", "name": "Examplecorp"}, "description": description,
               "jobLocation": {"@type": "Place", "address": {"addressLocality": "Pune", "addressCountry": "India"}}}
    return f'<html><head><script type="application/ld+json">{json.dumps(posting)}</script></head><body></body></html>'


def test_double_encoded_jsonld_posting_is_readable_and_raw_is_kept():
    raw = "&lt;p&gt;&lt;b&gt;Requirements&lt;/b&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Java and SQL&lt;/li&gt;&lt;/ul&gt;"
    (job,), warnings = extract_from_html(_page(raw), "https://careers.example.com/job/1", None)
    assert job.description_text == "Requirements\n\n• Java and SQL" and "<" not in job.description_text
    assert job.description_raw == raw, "the description as received is kept for provenance"


def test_normal_jsonld_posting_unchanged():
    job = posting_to_job({"title": "X", "description": "<p>Java &amp; SQL</p>"}, "https://e.example/j/1")
    assert job.description_text == "Java & SQL"


def test_cleaned_description_feeds_skill_extraction():
    from jobpilot.services.skills_taxonomy import extract_skills

    raw = "&lt;ul&gt;&lt;li&gt;Java and Spring Boot&lt;/li&gt;&lt;li&gt;PostgreSQL&lt;/li&gt;&lt;/ul&gt;"
    assert extract_skills(description_to_text(raw)) == ["java", "spring boot", "postgresql"]
