"""Text normalisation helpers shared by parsing, dedup and matching."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup

_WS = re.compile(r"\s+")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|gmbh|plc|pvt|private|technologies|technology|labs)\b\.?",
    re.I,
)


def clean_ws(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def normalize(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9+#./ ]+", " ", text.lower())
    return clean_ws(text)


def normalize_company(name: str | None) -> str:
    n = normalize(name)
    n = _COMPANY_SUFFIX.sub(" ", n)
    return clean_ws(re.sub(r"[./]", " ", n))


def normalize_title(title: str | None) -> str:
    t = normalize(title)
    t = re.sub(r"\((?:[^)]*)\)", " ", t)  # drop parentheticals like (remote)
    t = re.sub(r"\b(remote|hybrid|onsite|on site|urgent|hiring|immediate joiner)\b", " ", t)
    t = t.replace("sr.", "senior").replace("jr.", "junior")
    t = re.sub(r"\bsr\b", "senior", t)
    t = re.sub(r"\bjr\b", "junior", t)
    t = re.sub(r"\bsde\s*(1|i)\b", "sde i", t)
    return clean_ws(t)


def normalize_location(loc: str | None) -> str:
    return clean_ws(re.sub(r"[^a-z0-9 ]+", " ", normalize(loc)))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def html_to_text(html: str | None) -> str:
    """Convert HTML to readable text, preserving list items and paragraphs as lines."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section"]):
        block.insert_before("\n")
        block.insert_after("\n")
    text = soup.get_text()
    lines = [clean_ws(line) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line in ("", "•"):
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


_MARKUP_TAGS = r"(?:p|br|li|ul|ol|div|span|b|i|u|strong|em|h[1-6]|table|thead|tbody|tr|td|th|section|a|img|hr|font|blockquote)"
# Treated as HTML only with a closing tag or a line break: a lone "<div>" mentioned in prose stays text.
_MARKUP = re.compile(rf"</\s*{_MARKUP_TAGS}\s*>|<\s*br\s*/?\s*>", re.I)
_ENCODED_MARKUP = re.compile(rf"&lt;\s*/?\s*{_MARKUP_TAGS}\b", re.I)
_LEFTOVER_CLOSING_TAG = re.compile(rf"</\s*{_MARKUP_TAGS}\s*>", re.I)


def description_to_text(value: str | None) -> str:
    """Readable text of a job description that may be HTML, HTML encoded twice, or plain text.

    - HTML: converted once (``html_to_text``).
    - HTML encoded twice (``&lt;p&gt;…``, as some career sites publish it in JSON-LD): decoded a second time, but only
      when the value contains encoded tags *and* one decode still leaves real closing tags such as ``</p>``.
    - Plain text (no closing tag or ``<br>``): entities decoded and whitespace normalised, without the HTML parser, so
      literal text such as ``List<String>``, ``a < b`` or a lone ``<div>`` mentioned in prose is kept.
    """
    import html as _html

    if not value:
        return ""
    if not _MARKUP.search(value) and not _ENCODED_MARKUP.search(value):
        lines = [clean_ws(line) for line in _html.unescape(value).splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()
    text = html_to_text(value)
    if _ENCODED_MARKUP.search(value) and _LEFTOVER_CLOSING_TAG.search(text):
        text = html_to_text(text)
    return text


def contains_term(haystack_norm: str, term: str) -> bool:
    """Whole-word/phrase match of ``term`` in an already-normalised haystack."""
    t = normalize(term)
    if not t:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9+#])"
    return re.search(pattern, haystack_norm) is not None


def safe_url(url: str | None) -> str | None:
    """Return the URL only if it is an absolute http(s) URL; otherwise None.

    Imported data is untrusted: this prevents ``javascript:``/``data:`` links in the UI.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if len(url) > 1000 or not re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.I):
        return None
    return url
