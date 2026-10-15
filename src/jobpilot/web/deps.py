"""Shared web dependencies: DB sessions, templates, flash redirects, form parsing."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from jobpilot.config import Settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_dt(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (datetime, date)):
        return value.strftime(fmt)
    return str(value)


def _fmt_date(value: Any) -> str:
    return _fmt_dt(value, "%Y-%m-%d")


def _nl2br(value: str | None) -> Markup:
    if not value:
        return Markup("")
    return Markup("<br>").join(escape(value).split("\n"))


def _pct(value: Any, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["d"] = _fmt_date
templates.env.filters["nl2br"] = _nl2br
templates.env.filters["pct"] = _pct


def get_or_404(db: Session, model: type, obj_id: int, label: str | None = None):
    """Load a record by primary key or raise a 404 with a readable message."""
    obj = db.get(model, obj_id)
    if obj is None:
        name = label or re.sub(r"(?<!^)(?=[A-Z])", " ", model.__name__).lower()
        raise HTTPException(status_code=404, detail=f"{name.capitalize()} #{obj_id} not found")
    return obj


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def render(request: Request, name: str, status_code: int = 200, **ctx: Any):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    ctx["settings"] = request.app.state.settings
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


async def form_dict(request: Request) -> dict[str, Any]:
    """Return form fields; repeated keys become lists, blank strings become None."""
    form = await request.form()
    out: dict[str, Any] = {}
    for key in form.keys():
        values = form.getlist(key)
        if len(values) > 1:
            out[key] = [v for v in values if not isinstance(v, str) or v.strip() != ""]
        else:
            v = values[0] if values else None
            if isinstance(v, str):
                v = v.strip()
                v = v if v != "" else None
            out[key] = v
    return out


def first_error(exc: Exception) -> str:
    """Readable message from a pydantic ValidationError or other exception."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            e = errors()[0]
            loc = ".".join(str(x) for x in e.get("loc", ()) if x != "__root__")
            return f"{loc}: {e.get('msg')}" if loc else str(e.get("msg"))
        except Exception:  # pragma: no cover
            pass
    return str(exc)
