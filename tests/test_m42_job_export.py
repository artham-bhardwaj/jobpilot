"""Exporting jobs (title, company, location, apply link) as PDF and Excel from the Jobs page and the Job desk.
Synthetic data only."""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from pypdf import PdfReader

from jobpilot.services import job_export
from jobpilot.services import jobs as job_svc
from jobpilot.sources.base import JobData

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _add(db, title, url, ext, company="Examplecorp", location="Pune, India", score=80.0):
    job, _ = job_svc.upsert(db, None, JobData(title=title, company_name=company, location=location, apply_url=url,
                                              description_text="Java, SQL (synthetic).", external_id=ext), "manual")
    job.category, job.relevance_score = "java_backend", score
    return job


def _xlsx_rows(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        return [["".join(c.itertext()) for c in row.findall("m:c", NS)] for row in sheet.find("m:sheetData", NS)]


def _pdf_text(data: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def test_jobs_page_exports_all_filtered_jobs_as_excel_and_pdf(client, db):
    _add(db, "Backend Engineer A", "https://boards.example.com/jobs/a", "a")
    _add(db, "Java Developer B", "https://boards.example.com/jobs/b", "b", company="Beta Labs", score=70.0)
    _add(db, "Senior Staff Engineer", "https://boards.example.com/jobs/s", "s")  # outside the fresher focus
    db.commit()

    page = client.get("/jobs").text
    assert 'href="/jobs/export.pdf"' in page and 'href="/jobs/export.xlsx"' in page

    r = client.get("/jobs/export.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert re.search(r'attachment; filename="jobpilot-jobs-\d{4}-\d{2}-\d{2}\.xlsx"', r.headers["content-disposition"])
    rows = _xlsx_rows(r.content)
    assert rows[0] == ["#", "Job title", "Company", "Location", "Apply link"]
    assert rows[1] == ["1", "Backend Engineer A", "Examplecorp", "Pune, India", "https://boards.example.com/jobs/a"]
    assert rows[2][1:3] == ["Java Developer B", "Beta Labs"] and len(rows) == 3, "the page's filters apply"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "https://boards.example.com/jobs/b" in z.read("xl/worksheets/_rels/sheet1.xml.rels").decode()

    r = client.get("/jobs/export.pdf?q=beta")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = _pdf_text(r.content)
    assert "Java Developer B" in text and "Beta Labs" in text and "Backend Engineer A" not in text
    assert 'search "beta"' in text and "1 job" in text
    links = [a.get_object()["/A"]["/URI"] for p in PdfReader(io.BytesIO(r.content)).pages for a in p["/Annots"]]
    assert links == ["https://boards.example.com/jobs/b"], "the apply link is clickable"

    assert "Senior Staff Engineer" in _pdf_text(client.get("/jobs/export.pdf?level=any").content)
    assert client.get("/jobs/export.csv").status_code == 404


def test_desk_exports_the_current_tab(client, db):
    _add(db, "Backend Engineer A", "https://boards.example.com/jobs/a", "a")
    db.commit()
    page = client.get("/desk").text
    assert 'href="/desk/export.pdf"' in page and 'href="/desk/export.xlsx"' in page
    rows = _xlsx_rows(client.get("/desk/export.xlsx").content)
    assert [r[1] for r in rows[1:]] == ["Backend Engineer A"]
    assert "Backend Engineer A" in _pdf_text(client.get("/desk/export.pdf").content)
    assert len(_xlsx_rows(client.get("/desk/export.xlsx?tab=applied").content)) == 1, "header only"


def test_pdf_handles_many_rows_long_text_and_unsafe_links():
    rows = [job_export.Row(title=f"Engineer {i} " + "very long title words " * 8, company="Café Ünïcode 株式会社",
                           location="Bengaluru, India (Remote)", link=f"https://example.com/{'x' * 300}/{i}")
            for i in range(120)]
    data = job_export.to_pdf(rows, title="JobPilot: Jobs", subtitle="test")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) > 3 and "Page 1 of" in reader.pages[0].extract_text()
    assert "Cafe" in reader.pages[0].extract_text() or "Café" in reader.pages[0].extract_text()
    assert job_export.safe_link("javascript:alert(1)") == "" and job_export.safe_link("file:///etc/passwd") == ""
    assert job_export.safe_link("https://example.com/jobs/é") == "https://example.com/jobs/%C3%A9"
    empty = PdfReader(io.BytesIO(job_export.to_pdf([], subtitle="none")))
    assert "No jobs match" in empty.pages[0].extract_text()
