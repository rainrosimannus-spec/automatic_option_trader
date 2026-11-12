"""
Account statements — /reports

One page, two independent accounts. The Options tab reports on the wheel
account and the Portfolio tab on the compounder account; each generates its own
PDF from its own Flex archive. Nothing is ever combined across the two.

    GET  /reports                          the page (?side= selects the tab)
    POST /reports/{side}/import            fetch the Flex query (background)
    POST /reports/{side}/upload            archive an XML downloaded from IBKR
    GET  /reports/{side}/statement.pdf     generate + download (bookmarkable)
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from src.core.logger import get_logger
from src.reports import SIDES
from src.reports import account_statement as stmt
from src.reports.flex_archive import (
    archive_status, import_from_xml, import_from_api,
)
from src.web.template_engine import templates

log = get_logger(__name__)

router = APIRouter()

# Last import outcome per side. The API import polls IBKR for up to two minutes,
# so it runs on a thread and the page reads the result from here rather than
# holding the request open.
_last_import: dict[str, dict] = {}
_import_running: dict[str, bool] = {}


def _check_side(side: str) -> str:
    if side not in SIDES:
        raise HTTPException(status_code=404, detail=f"unknown side {side!r}")
    return side


def _parse_date(s: Optional[str], field: str) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"{field} must be YYYY-MM-DD, got {s!r}")


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, side: str = Query("options")):
    side = _check_side(side)
    today = date.today()
    presets = stmt.period_presets(today)

    ctx = {
        "request": request,
        "side": side,
        "sides": [{"key": s, "label": stmt.SIDE_LABELS[s]} for s in SIDES],
        "today": today,
        "presets": [{
            "key": p["key"],
            "label": p["label"],
            "start": p["start"].isoformat() if p["start"] else "",
            "end": p["end"].isoformat(),
            "as_of": [d.isoformat() for d in p["as_of"]],
        } for p in presets],
        "archive": archive_status(side),
        "last_import": _last_import.get(side),
        "import_running": _import_running.get(side, False),
        "default_as_of_1": date(today.year - 1, 12, 31).isoformat(),
        "default_as_of_2": today.isoformat(),
    }
    return templates.TemplateResponse("reports.html", ctx)


@router.post("/reports/{side}/import")
def import_flex(side: str):
    """Pull the configured Flex query and archive it. Runs on a thread — IBKR's
    statement generation is a poll loop that can take a minute or two."""
    side = _check_side(side)
    if _import_running.get(side):
        return RedirectResponse(url=f"/reports?side={side}", status_code=303)

    def _run():
        _import_running[side] = True
        try:
            result = import_from_api(side)
            _last_import[side] = {
                "ok": True,
                "at": datetime.now(),
                "message": (
                    f"Imported {result['statements']} statement(s) covering "
                    f"{result['range'][0] or '?'} → {result['range'][1] or '?'}: "
                    f"{result['events_added']} new transaction(s), "
                    f"{result['events_updated']} updated, "
                    f"{result['positions']} position row(s)."
                ),
            }
        except Exception as exc:
            log.warning("flex_import_failed", side=side, error=str(exc))
            _last_import[side] = {"ok": False, "at": datetime.now(), "message": str(exc)}
        finally:
            _import_running[side] = False

    threading.Thread(target=_run, daemon=True).start()
    return RedirectResponse(url=f"/reports?side={side}", status_code=303)


@router.post("/reports/{side}/upload")
async def upload_flex(side: str, xml_file: UploadFile = File(...)):
    """Archive a Flex statement downloaded by hand from Account Management.

    This is the path that covers history predating the archive — the Flex API
    can only serve the period baked into the saved query.
    """
    side = _check_side(side)
    raw = await xml_file.read()
    try:
        xml_content = raw.decode("utf-8")
    except UnicodeDecodeError:
        xml_content = raw.decode("latin-1", errors="replace")

    try:
        result = import_from_xml(side, xml_content, source="upload",
                                 filename=xml_file.filename or "")
        _last_import[side] = {
            "ok": True,
            "at": datetime.now(),
            "message": (
                f"{xml_file.filename}: {result['statements']} statement(s) covering "
                f"{result['range'][0] or '?'} → {result['range'][1] or '?'}, "
                f"{result['events_added']} new transaction(s), "
                f"{result['events_updated']} updated, "
                f"{result['positions']} position row(s)."
            ),
        }
    except Exception as exc:
        log.warning("flex_upload_failed", side=side, error=str(exc))
        _last_import[side] = {"ok": False, "at": datetime.now(),
                              "message": f"{xml_file.filename}: {exc}"}
    return RedirectResponse(url=f"/reports?side={side}", status_code=303)


@router.get("/reports/{side}/statement.pdf")
def statement_pdf(
    side: str,
    preset: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    as_of: list[str] = Query(default=[]),
):
    """Generate the account statement PDF and return it.

    Either `preset` (last_year | this_year | all_time) or an explicit
    start/end pair. `as_of` may be repeated for several holdings snapshots.
    """
    side = _check_side(side)
    today = date.today()

    if preset:
        try:
            p = stmt.resolve_preset(side, preset, today)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        start_d, end_d = p["start"], p["end"]
        as_of_dates = p["as_of"]
    else:
        start_d = _parse_date(start, "start")
        end_d = _parse_date(end, "end") or today
        if start_d is None:
            from src.reports.flex_archive import earliest_activity
            start_d = earliest_activity(side) or date(today.year, 1, 1)
        as_of_dates = [d for d in (_parse_date(a, "as_of") for a in as_of) if d]
        if not as_of_dates:
            as_of_dates = [date(today.year - 1, 12, 31), today]

    if end_d < start_d:
        raise HTTPException(status_code=400, detail="end date is before start date")

    # De-dupe and order the holdings dates so a form that posts the same date
    # twice doesn't produce two identical sections.
    as_of_dates = sorted(set(as_of_dates))

    path, data = stmt.generate(side, start_d, end_d, as_of_dates)
    filename = (f"{side}-{data.account_id or 'account'}-"
                f"{start_d.isoformat()}_{end_d.isoformat()}.pdf")
    return FileResponse(str(path), media_type="application/pdf", filename=filename)


_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+\.pdf")


@router.get("/reports/{side}/archive/{filename}")
def download_generated(side: str, filename: str):
    """Re-download a previously generated statement."""
    side = _check_side(side)
    if not _SAFE_NAME.fullmatch(filename):
        raise HTTPException(status_code=400, detail="bad filename")
    path = stmt.STATEMENTS_DIR / side / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path), media_type="application/pdf", filename=filename)
