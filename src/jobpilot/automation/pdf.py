"""HTML → PDF using a locally installed Chrome/Chromium via Playwright (optional)."""

from __future__ import annotations

from jobpilot.config import Settings


class PdfUnavailable(RuntimeError):
    pass


def html_to_pdf(html: str, settings: Settings) -> bytes:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PdfUnavailable("playwright is not installed (pip install -e '.[browser]')") from exc
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel=settings.browser_channel or None, headless=True)
            except PlaywrightError:
                browser = pw.chromium.launch(headless=True)  # bundled chromium, if installed
            try:
                page = browser.new_page()
                # Offline render: block any network access from the document.
                page.route("**/*", lambda route: route.abort() if not route.request.url.startswith("data:") else route.continue_())
                page.set_content(html, wait_until="load")
                return page.pdf(format="A4", margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"},
                                print_background=True)
            finally:
                browser.close()
    except PdfUnavailable:
        raise
    except Exception as exc:
        raise PdfUnavailable(f"browser unavailable for PDF rendering: {exc}") from exc
