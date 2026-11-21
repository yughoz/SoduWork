"""Nekowork MCP: bearer token management + the 6 Super MCP tools (PRD modul_mcp).

Tool contracts mirror Nekorp's Super MCP so existing clients (Hermes, Maus,
Cursor) keep their prompts. GitLab tools are not ported (PRD out of scope).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import nekowork_asana
from ..nekowork_models import McpToken, UserIntegration
from .nekowork_newapi import NewApiError

TOKEN_PREFIX = "nwmcp_"
RATE_LIMIT = 60          # requests per minute per token
_WINDOW: dict[int, deque] = defaultdict(deque)


# ---------------------------------------------------------------- tokens
def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_token(session: AsyncSession, *, name: str) -> dict:
    existing = (await session.execute(select(McpToken).where(McpToken.name == name))).scalar_one_or_none()
    if existing:
        raise NewApiError(f"Nama token '{name}' sudah dipakai", 409)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(24)
    row = McpToken(name=name, token_hash=_hash(raw))
    session.add(row)
    await session.flush()
    return {"id": row.id, "name": name, "token": raw}


async def list_tokens(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(McpToken).order_by(McpToken.id))).scalars().all()
    return [{"id": r.id, "name": r.name, "enabled": r.enabled,
             "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


async def disable_token(session: AsyncSession, token_id: int) -> bool:
    row = (await session.execute(select(McpToken).where(McpToken.id == token_id))).scalar_one_or_none()
    if not row:
        return False
    row.enabled = False
    return True


async def authenticate(session: AsyncSession, authorization: str | None):
    """Bearer -> enabled McpToken, or None. Raises on rate limit overflow."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    row = (await session.execute(select(McpToken).where(
        McpToken.token_hash == _hash(authorization[7:].strip()),
        McpToken.enabled.is_(True)))).scalar_one_or_none()
    if not row:
        return None
    now = time.monotonic()
    window = _WINDOW[row.id]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        raise NewApiError("Rate limit MCP terlampaui (60 req/menit)", 429)
    window.append(now)
    row.last_used_at = datetime.now(timezone.utc)
    return row


def mcp_actor(token: McpToken) -> SimpleNamespace:
    """Pseudo-user for audit rows written on behalf of an MCP client."""
    return SimpleNamespace(id=None, username=f"mcp:{token.name}",
                           display_name=f"mcp:{token.name}", org_id=None)


# ---------------------------------------------------------------- tools
async def _hub_user(session: AsyncSession, ref: str):
    """Resolve a hub user by id, email, or username."""
    from ..models import User
    q = select(User)
    row = (await session.execute(q.where(User.id == ref))).scalar_one_or_none()
    if row:
        return row
    row = (await session.execute(q.where(User.email == ref.lower()))).scalar_one_or_none()
    if row:
        return row
    return (await session.execute(q.where(User.username == ref))).scalar_one_or_none()


async def list_employees(session: AsyncSession, provider: str = "asana") -> list[dict]:
    """Hub users with their external identity mapping (contract = Nekorp)."""
    from ..models import User
    rows = (await session.execute(
        select(User, UserIntegration).outerjoin(
            UserIntegration,
            (UserIntegration.user_id == User.id) & (UserIntegration.provider == provider))
        .order_by(User.display_name))).all()
    return [{
        "user": u.id, "name": u.display_name or u.username, "email": u.email or "",
        "external_id": (i.external_id if i else None),
        "external_name": (i.provider_name if i else None),
        "provider": provider, "status": (i.status if i else None),
    } for u, i in rows]


async def search_tasks(session: AsyncSession, *, query: str | None, assignee: str | None,
                       completed: bool | None) -> list[dict]:
    """Live Asana search through Maton (Nekorp cached it; v1 searches directly)."""
    cfg = nekowork_asana._maton()
    params: dict = {"limit": 50, "opt_fields": "name,completed,assignee.name,permalink_url,due_on"}
    if query:
        params["text"] = query
    if assignee:
        user = await _hub_user(session, assignee)
        if not user:
            raise NewApiError(f"User '{assignee}' tidak ditemukan", 404)
        info = await nekowork_asana.resolve_assignee(session, user.id)
        params["assignee.any"] = info["gid"]
    if completed is not None:
        params["completed"] = "true" if completed else "false"
    payload = await nekowork_asana._request(
        "GET", f"/workspaces/{cfg['workspace']}/tasks/search", params=params)
    return [{"gid": t.get("gid"), "name": t.get("name"), "completed": bool(t.get("completed")),
             "assignee": ((t.get("assignee") or {}).get("name") if t.get("assignee") else None),
             "url": t.get("permalink_url") or "", "due_on": t.get("due_on") or None}
            for t in (payload.get("data") or [])]


async def set_identity(session: AsyncSession, actor, *, user: str, provider: str,
                       external_id: str, provider_email: str | None = None,
                       provider_name: str | None = None) -> dict:
    if provider != "asana":
        raise NewApiError(f"Provider '{provider}' belum didukung (v1: asana)", 422)
    hub_user = await _hub_user(session, user)
    if not hub_user:
        raise NewApiError(f"User '{user}' tidak ditemukan", 404)
    return await nekowork_asana.set_identity(session, actor, user_id=hub_user.id,
                                             external_id=external_id,
                                             provider_email=provider_email,
                                             provider_name=provider_name)


async def delete_identity(session: AsyncSession, actor, *, user: str, provider: str) -> dict:
    if provider != "asana":
        raise NewApiError(f"Provider '{provider}' belum didukung (v1: asana)", 422)
    hub_user = await _hub_user(session, user)
    if not hub_user:
        raise NewApiError(f"User '{user}' tidak ditemukan", 404)
    row = (await session.execute(select(UserIntegration).where(
        UserIntegration.user_id == hub_user.id, UserIntegration.provider == provider))).scalar_one_or_none()
    if not row:
        raise NewApiError("Mapping tidak ditemukan", 404)
    await session.delete(row)
    return {"removed": True, "user": hub_user.id, "provider": provider}


async def create_asana_task(session: AsyncSession, actor, args: dict) -> dict:
    """MCP writes are AI writes: project gating (ai_enabled) enforced."""
    params = {k: args.get(k) for k in ("name", "notes", "assignee_user", "project_gid", "due_on")}
    if not params.get("name"):
        raise NewApiError("Field 'name' wajib", 422)
    if args.get("assignee_user"):
        hub_user = await _hub_user(session, str(args["assignee_user"]))
        if not hub_user:
            raise NewApiError(f"User '{args['assignee_user']}' tidak ditemukan", 404)
        params["assignee_user"] = hub_user.id
    return await nekowork_asana.create_task(session, actor, params, for_ai=True)


async def complete_asana_task(session: AsyncSession, actor, args: dict) -> dict:
    task_gid = str(args.get("task_gid") or "")
    if not task_gid:
        raise NewApiError("Field 'task_gid' wajib", 422)
    return await nekowork_asana.set_task_completed(session, actor, task_gid,
                                                   bool(args.get("completed", True)))


# ---------------------------------------------------------------- schema
TOOLS = [
    {"name": "list_employees", "description": "Daftar karyawan Hub + mapping Asana",
     "inputSchema": {"type": "object", "properties": {"provider": {"type": "string", "default": "asana"}}}},
    {"name": "search_tasks", "description": "Cari task Asana (query, assignee = user Hub, completed)",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "assignee": {"type": "string"},
         "completed": {"type": "boolean"}}}},
    {"name": "set_identity", "description": "Set mapping karyawan Hub ke identitas eksternal",
     "inputSchema": {"type": "object", "properties": {
         "user": {"type": "string"}, "provider": {"type": "string"},
         "external_id": {"type": "string"}, "provider_email": {"type": "string"},
         "provider_name": {"type": "string"}},
         "required": ["user", "provider", "external_id"]}},
    {"name": "delete_identity", "description": "Hapus mapping karyawan Hub",
     "inputSchema": {"type": "object", "properties": {
         "user": {"type": "string"}, "provider": {"type": "string"}},
         "required": ["user", "provider"]}},
    {"name": "create_asana_task", "description": "Buat task Asana (project harus ai_enabled)",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "notes": {"type": "string"},
         "assignee_user": {"type": "string"}, "project_gid": {"type": "string"},
         "due_on": {"type": "string"}},
         "required": ["name"]}},
    {"name": "complete_asana_task", "description": "Selesaikan/buka task Asana",
     "inputSchema": {"type": "object", "properties": {
         "task_gid": {"type": "string"}, "completed": {"type": "boolean"}},
         "required": ["task_gid"]}},
]
