"""Nekowork usage dashboard: 14-day daily usage from New API consume logs.

Source of truth is New API; the Hub only aggregates and caches for 5 minutes
(PRD modul_token_dashboard). Dates are bucketed in Asia/Jakarta, matching the
values users see on the New API dashboard.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from . import nekowork_newapi as gwsvc
from .nekowork_newapi import NewApiError, quota_to_usd

JAKARTA = timezone(timedelta(hours=7))
CACHE_TTL = 300  # seconds
_PAGE_SIZE = 100
_MAX_PAGES = 30

_cache: dict[str, tuple[float, object]] = {}


def _date_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, JAKARTA).strftime("%Y-%m-%d")


def _day_bounds(date_key: str) -> tuple[float, float]:
    start = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=JAKARTA)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


async def _fetch_consume_logs(days: int, *, force: bool) -> list[dict]:
    """All type=2 (consume) logs inside the last `days` Jakarta days.

    /api/log is sorted id DESC (newest first); stop paging once the oldest
    entry on a page is older than the window start.
    """
    cached = _cache.get("logs")
    if cached and not force and time.time() - cached[0] < CACHE_TTL:
        return cached[1]  # type: ignore[return-value]

    gw = gwsvc._gateway()
    window_start = _day_bounds(_date_key(time.time()))[0] - (days - 1) * 86400
    logs: list[dict] = []
    for page in range(_MAX_PAGES):
        payload = await _paged_request(gw, page)
        items = gwsvc._items(payload)
        if not items:
            break
        logs.extend(items)
        oldest = min((int(it.get("created_at") or 0) for it in items if it.get("created_at")), default=0)
        if len(items) < _PAGE_SIZE or (oldest and oldest < window_start):
            break
    cutoff = int(window_start)
    logs = [it for it in logs if int(it.get("created_at") or 0) >= cutoff]
    _cache["logs"] = (time.time(), logs)
    return logs


async def _paged_request(gw, page: int) -> object:
    """GET /api/log with 429 backoff, mirroring Nekorp's rate-limit handling."""
    for attempt in range(3):
        try:
            return await gwsvc._request(gw, "GET", "/api/log/", params={
                "p": page, "page_size": _PAGE_SIZE, "type": 2,
            })
        except NewApiError as exc:
            if exc.status == 429 and attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            raise
    return {"data": []}


def _aggregate(logs: list[dict], *, gateway_username: str, days: int) -> dict:
    """Daily rows (oldest → today), today/month/14d totals for one user."""
    today = _date_key(time.time())
    month_start = today[:8] + "01"
    per_day: dict[str, dict] = {}
    for log in logs:
        if str(log.get("username") or "") != gateway_username:
            continue
        key = _date_key(int(log.get("created_at") or 0))
        bucket = per_day.setdefault(key, {"requests": 0, "tokens": 0, "quota": 0})
        bucket["requests"] += 1
        bucket["tokens"] += int(log.get("prompt_tokens") or 0) + int(log.get("completion_tokens") or 0)
        bucket["quota"] += int(log.get("quota") or 0)
    rows = []
    for i in range(days - 1, -1, -1):
        key = _date_key(time.time() - i * 86400)
        bucket = per_day.get(key, {"requests": 0, "tokens": 0, "quota": 0})
        rows.append({"date": key, "requests": bucket["requests"], "tokens": bucket["tokens"],
                     "usd": quota_to_usd(bucket["quota"]) or 0.0})
    month_rows = [b for k, b in per_day.items() if k >= month_start]
    return {
        "days": rows,
        "today_usd": next((r["usd"] for r in rows if r["date"] == today), 0.0),
        "month_usd": quota_to_usd(sum(b["quota"] for b in month_rows)) or 0.0,
        "window_usd": round(sum(r["usd"] for r in rows), 6),
        "window_days": days,
    }


async def usage_for(gateway_username: str, *, days: int = 14, force: bool = False) -> dict:
    logs = await _fetch_consume_logs(days, force=force)
    return _aggregate(logs, gateway_username=gateway_username, days=days)


async def usage_all(session, *, days: int = 14, force: bool = False) -> list[dict]:
    """Admin summary: usage rows per provisioned user plus totals (PRD §6)."""
    accounts = await gwsvc.list_all_accounts(session)
    logs = await _fetch_consume_logs(days, force=force)
    out = []
    for acc in accounts:
        usage = _aggregate(logs, gateway_username=acc["username"], days=days)
        out.append({**acc, "usage": usage})
    out.sort(key=lambda r: r["usage"]["month_usd"], reverse=True)
    return out
