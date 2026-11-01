"""Export a list of jobs (title, company, location, apply link) as Excel (.xlsx) or PDF, to share with friends.

Both formats are written with the standard library only (no extra dependencies): the .xlsx is a minimal
Office Open XML workbook, and the PDF uses the built-in Helvetica font with clickable links.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote
from xml.sax.saxutils import escape

MAX_ROWS = 5000
HEADERS = ("#", "Job title", "Company", "Location", "Apply link")


@dataclass(frozen=True)
class Row:
    title: str
    company: str
    location: str
    link: str


def safe_link(url: str | None) -> str:
    """Only http(s) links are exported (never javascript: or file: URLs); non-ASCII is percent-encoded."""
    url = (url or "").strip()
    if not re.match(r"(?i)^https?://", url):
        return ""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")


def rows_for(jobs) -> list[Row]:
    out = []
    for j in jobs:
        remote = {"remote": "Remote", "hybrid": "Hybrid"}.get(j.remote_type or "")
        loc = j.location or ""
        if remote and remote.lower() not in loc.lower():
            loc = f"{loc} ({remote})" if loc else remote
        out.append(Row(title=j.title or "", company=j.company_name or "", location=loc,
                       link=safe_link(j.apply_url) or safe_link(j.source_url)))
    return out


def filename(stem: str, ext: str) -> str:
    return f"{stem}-{date.today().isoformat()}.{ext}"


# ---- Excel ----------------------------------------------------------------------------------------------------

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _x(s: str) -> str:
    return escape(_XML_BAD.sub("", s), {'"': "&quot;"})


def _col(i: int) -> str:
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]


def to_xlsx(rows: list[Row], title: str = "Jobs") -> bytes:
    cells = []

    def cell(ref: str, text: str, style: int = 0) -> str:
        return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{_x(text[:32000])}</t></is></c>'

    cells.append('<row r="1">' + "".join(cell(f"{_col(i)}1", h, 1) for i, h in enumerate(HEADERS)) + "</row>")
    links = []
    for n, r in enumerate(rows, start=1):
        rn = n + 1
        num = f'<c r="A{rn}" s="0"><v>{n}</v></c>'
        vals = [cell(f"B{rn}", r.title), cell(f"C{rn}", r.company), cell(f"D{rn}", r.location),
                cell(f"E{rn}", r.link, 2 if r.link else 0)]
        cells.append(f'<row r="{rn}">{num}{"".join(vals)}</row>')
        if r.link and len(r.link) <= 2000:  # Excel ignores longer hyperlinks
            links.append((f"E{rn}", r.link))
    last = len(rows) + 1
    hyperlinks = ""
    rels = ""
    if links:
        hyperlinks = "<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i}"/>'
                                              for i, (ref, _) in enumerate(links, start=1)) + "</hyperlinks>"
        rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                          f'relationships/hyperlink" Target="{_x(url)}" TargetMode="External"/>'
                          for i, (_, url) in enumerate(links, start=1))
                + "</Relationships>")
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
             'state="frozen"/></sheetView></sheetViews>'
             '<cols><col min="1" max="1" width="6" customWidth="1"/><col min="2" max="2" width="48" customWidth="1"/>'
             '<col min="3" max="3" width="28" customWidth="1"/><col min="4" max="4" width="28" customWidth="1"/>'
             '<col min="5" max="5" width="70" customWidth="1"/></cols>'
             f'<sheetData>{"".join(cells)}</sheetData>'
             f'<autoFilter ref="A1:E{last}"/>{hyperlinks}</worksheet>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font>'
              '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/>'
              '</fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF7"/></patternFill></fill></fills>'
              '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="3"><xf fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
              '<xf fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    sheet_name = _x(re.sub(r"[\\/*?:\[\]]", " ", title)[:31] or "Jobs")
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.styles+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"'{sheet_name}'!$A$1:$E${last}</definedName></definedNames></workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'styles" Target="styles.xml"/></Relationships>'),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/styles.xml": styles,
    }
    if rels:
        files["xl/worksheets/_rels/sheet1.xml.rels"] = rels
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---- PDF ------------------------------------------------------------------------------------------------------

# Helvetica glyph widths (1/1000 em) for ASCII 32..126, from the standard AFM metrics.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]

PAGE_W, PAGE_H, MARGIN = 842.0, 595.0, 36.0  # A4 landscape
COLS = (("#", 26), ("Job title", 236), ("Company", 150), ("Location", 128), ("Apply link", 230))
FONT, LEAD, PAD = 9.0, 11.0, 4.0
MAX_LINES = {1: 3, 2: 2, 3: 2, 4: 3}


def _pdf_text(s: str) -> str:
    """To characters Helvetica (WinAnsi) can show: accents are dropped when needed, anything else becomes '?'."""
    out = []
    for ch in s.replace("\r", " ").replace("\n", " ").replace("\t", " "):
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(base or "?")
    return "".join(out)


def _w(s: str, size: float, bold: bool = False) -> float:
    w = sum(_HELV[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in s) * size / 1000
    return w * 1.08 if bold else w


def _wrap(text: str, width: float, max_lines: int, size: float = FONT, hard: bool = False) -> list[str]:
    """Wrap on spaces (or anywhere, for URLs), with an ellipsis if it runs past ``max_lines``."""
    lines: list[str] = []
    rest = text.strip()
    while rest and len(lines) < max_lines:
        if _w(rest, size) <= width:
            lines.append(rest)
            rest = ""
            break
        cut = len(rest)
        while cut > 1 and _w(rest[:cut], size) > width:
            cut -= 1
        if not hard:
            sp = rest.rfind(" ", 0, cut + 1)
            if sp > 0:
                cut = sp
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest and lines:
        last = lines[-1]
        while last and _w(last + "...", size) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return lines or [""]


def _esc(s: str) -> bytes:
    b = s.encode("cp1252", "replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def to_pdf(rows: list[Row], title: str = "Jobs", subtitle: str = "") -> bytes:
    pages: list[tuple[list[bytes], list[tuple[tuple[float, float, float, float], str]]]] = []
    ops: list[bytes] = []
    annots: list[tuple[tuple[float, float, float, float], str]] = []
    y = 0.0
    table_w = sum(w for _, w in COLS)

    def text(x: float, ty: float, s: str, size: float = FONT, bold: bool = False, rgb=(0, 0, 0)) -> None:
        ops.append(b"%.3f %.3f %.3f rg BT /%s %.1f Tf %.2f %.2f Td (" % (*rgb, b"F2" if bold else b"F1", size, x, ty)
                   + _esc(s) + b") Tj ET")

    def header_row() -> None:
        nonlocal y
        h = LEAD + 2 * PAD
        ops.append(b"0.91 0.93 0.97 rg %.2f %.2f %.2f %.2f re f" % (MARGIN, y - h, table_w, h))
        x = MARGIN
        for label, w in COLS:
            text(x + PAD, y - PAD - FONT + 1, label, bold=True)
            x += w
        y -= h

    def new_page(first: bool) -> None:
        nonlocal ops, annots, y
        ops, annots = [], []
        pages.append((ops, annots))
        y = PAGE_H - MARGIN
        if first:
            text(MARGIN, y - 16, _pdf_text(title), 16, bold=True)
            y -= 24
            if subtitle:
                text(MARGIN, y - 10, _pdf_text(subtitle)[:180], 9, rgb=(0.35, 0.35, 0.35))
                y -= 18
            y -= 4
        header_row()

    new_page(True)
    if not rows:
        text(MARGIN + PAD, y - PAD - FONT, "No jobs match these filters.", rgb=(0.35, 0.35, 0.35))
    for n, r in enumerate(rows, start=1):
        values = [str(n), _pdf_text(r.title), _pdf_text(r.company), _pdf_text(r.location), r.link]
        cells = [[values[0]]] + [_wrap(values[i], COLS[i][1] - 2 * PAD, MAX_LINES[i], hard=(i == 4))
                                 for i in range(1, 5)]
        h = max(len(c) for c in cells) * LEAD + 2 * PAD
        if y - h < MARGIN + 18:
            new_page(False)
        if n % 2 == 0:
            ops.append(b"0.97 0.97 0.98 rg %.2f %.2f %.2f %.2f re f" % (MARGIN, y - h, table_w, h))
        x = MARGIN
        for i, ((_, w), lines) in enumerate(zip(COLS, cells)):
            colour = (0.02, 0.39, 0.76) if i == 4 else (0, 0, 0)
            for k, line in enumerate(lines):
                text(x + PAD, y - PAD - FONT + 1 - k * LEAD, line, rgb=colour)
            if i == 4 and r.link:
                annots.append(((x, y - h, x + w, y), r.link))
            x += w
        ops.append(b"0.85 0.85 0.85 RG 0.5 w %.2f %.2f m %.2f %.2f l S" % (MARGIN, y - h, MARGIN + table_w, y - h))
        y -= h

    total = len(pages)
    for i, (page_ops, _) in enumerate(pages, start=1):
        label = f"Page {i} of {total}"
        page_ops.append(b"0.5 0.5 0.5 rg BT /F1 8 Tf %.2f %.2f Td (" % (PAGE_W - MARGIN - _w(label, 8), MARGIN - 14)
                        + _esc(label) + b") Tj ET")

    # Objects: 1 catalog, 2 pages, 3 Helvetica, 4 Helvetica-Bold, 5 info, then per page: page, content, links.
    objs: dict[int, bytes] = {
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        5: b"<< /Title (" + _esc(_pdf_text(title)) + b") /Producer (JobPilot) >>",
    }
    nxt = 6
    kids = []
    for page_ops, page_annots in pages:
        page_id, content_id = nxt, nxt + 1
        nxt += 2
        annot_ids = []
        for (x1, y1, x2, y2), url in page_annots:
            objs[nxt] = (b"<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] "
                         b"/A << /S /URI /URI (" % (x1, y1, x2, y2) + _esc(url) + b") >> >>")
            annot_ids.append(nxt)
            nxt += 1
        stream = b"\n".join(page_ops)
        objs[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        annots_ref = (b" /Annots [" + b" ".join(b"%d 0 R" % a for a in annot_ids) + b"]") if annot_ids else b""
        objs[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                         b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >>" % (PAGE_W, PAGE_H, content_id)
                         + annots_ref + b" >>")
        kids.append(page_id)
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for oid in range(1, nxt):
        offsets[oid] = out.tell()
        out.write(b"%d 0 obj\n" % oid + objs[oid] + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % nxt)
    for oid in range(1, nxt):
        out.write(b"%010d 00000 n \n" % offsets[oid])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (nxt, xref))
    return out.getvalue()
