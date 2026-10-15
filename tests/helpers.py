"""Test helpers: offline HTTP stub and synthetic profile builders."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import requests

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession(requests.Session):
    """requests.Session that serves canned responses; never touches the network."""

    def __init__(self, routes: dict[str, tuple[int, bytes | str, dict] | tuple[int, bytes | str]]):
        super().__init__()
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None, **kwargs):  # noqa: D401 - mimic requests
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            for prefix, value in self.routes.items():
                if prefix.endswith("*") and url.startswith(prefix[:-1]):
                    route = value
                    break
        resp = requests.Response()
        resp.url = url
        if route is None:
            resp.status_code, resp._content = 404, b"not found"
            resp._content_consumed = True
            return resp
        if isinstance(route, BaseException):
            raise route  # simulate timeouts / connection errors
        if isinstance(route, list):  # a sequence of responses for successive calls
            route = route.pop(0) if len(route) > 1 else route[0]
            if isinstance(route, BaseException):
                raise route
        status, body, *rest = route
        resp.status_code = status
        resp._content = body.encode() if isinstance(body, str) else body
        resp.headers.update(rest[0] if rest else {})
        resp.encoding = "utf-8"
        resp._content_consumed = True  # lets iter_content() replay the canned body (streaming reads)
        return resp


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def make_http(settings, routes):
    from jobpilot.sources.http import PoliteHttp

    return PoliteHttp(settings, session=FakeSession(routes))


def seed_profile(session, *, years: float | None = 1.5, skills=("java", "spring boot", "sql", "rest api", "python"),
                 learning=("kafka",), locations=("Bengaluru",), needs_sponsorship=False, confirm=True):
    """Create a clearly synthetic candidate profile ("Test Candidate")."""
    from jobpilot.services import profile as ps

    ps.update_profile(session, ps.ProfileIn(
        full_name="Test Candidate", first_name="Test", last_name="Candidate", email="test.candidate@example.com",
        phone="+91 90000 00000", current_location="Bengaluru, India", total_experience_years=years,
        acceptable_locations=list(locations), needs_sponsorship=needs_sponsorship, willing_to_relocate=False,
        headline="Backend engineer (synthetic test profile)"))
    if confirm:
        ps.confirm_profile(session)
    for s in skills:
        ps.save_fact(session, "skills", ps.SkillIn(name=s, status="confirmed"))
    for s in learning:
        ps.save_fact(session, "skills", ps.SkillIn(name=s, status="learning"))
    ps.save_fact(session, "employment", ps.EmploymentIn(
        company="Synthetic Corp", title="Software Engineer", employment_type="full_time",
        start_date=date(2024, 1, 1), description="Built Java services (synthetic).", status="confirmed"))
    ps.save_fact(session, "education", ps.EducationIn(
        institution="Example Institute of Technology", degree="B.Tech", field_of_study="Computer Science",
        status="confirmed"))
    session.flush()


def make_pdf_bytes(text_lines: list[str]) -> bytes:
    """Build a tiny valid PDF with the given text lines (no external deps)."""
    content = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(
        "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for line in text_lines
    ) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class LocalPage:
    """Serve one synthetic HTML page on 127.0.0.1 (for headless-browser tests; no external network)."""

    def __init__(self, html: str):
        import http.server
        import threading

        body = html.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/apply"
        self.host = "127.0.0.1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
