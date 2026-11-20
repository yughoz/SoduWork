"""Nekowork Asana integration via Maton (PRD modul_asana).

Cache mirrors (users/projects) + identity mapping + task write. All Asana
writes go through Maton with the connection header; cache rows are mirrors,
never the source of truth for task existence.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..nekowork_models import AsanaProjectCache, AsanaUserCache, UserIntegration
from .nekowork_newapi import NewApiError

log = logging.getLogger(__name__)


class AsanaError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _maton() -> dict:
    s = get_settings()
    if not (s.maton_api_key and s.maton_asana_connection_id and s.asana_workspace_gid):
        raise AsanaError("Asana belum dikonfigurasi (MATON_API_KEY / connection / workspace)", 503)
    return {
        "base": s.maton_base_url.rstrip("/"),
        "headers": {
            "Authorization": f"Bearer {s.maton_api_key}",
            "Maton-Connection": s.maton_asana_connection_id,
            "Content-Type": "application/json",
        },
        "workspace": s.asana_workspace_gid,
    }


async def _request(method: str, path: str, *, params: dict | None = None,
                   json_body: dict | None = None) -> dict:
    cfg = _maton()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, cfg["base"] + path, params=params,
                                        json=json_body, headers=cfg["headers"])
    except httpx.HTTPError as exc:
        raise AsanaError(f"Asana/Maton tidak terjangkau: {exc}") from exc
    if resp.status_code >= 400:
        raise AsanaError(f"Asana HTTP {resp.status_code}: {resp.text[:200]}", 502)
    try:
        return resp.json()
    except ValueError as exc:
        raise AsanaError("Asana/Maton balas bukan JSON") from exc


async def _paged(cfg_workspace: str, path: str) -> list[dict]:
    """Asana list endpoints with offset pagination (limit 100 per page)."""
    out: list[dict] = []
    offset: str | None = None
    for _ in range(20):
        params: dict = {"workspace": cfg_workspace, "limit": 100, "opt_fields": "name,email,photo,archived"}
        if offset:
            params["offset"] = offset
        payload = await _request("GET", path, params=params)
        out.extend(payload.get("data") or [])
        offset = (payload.get("next_page") or {}).get("offset")
        if not offset:
            break
    return out


async def sync_cache(session: AsyncSession) -> dict:
    """Refresh user + project caches (idempotent upsert by gid). Nekowork-owned
    gates (ai_enabled/keywords/ai_notes) survive the refresh."""
    cfg = _maton()
    ws = cfg["workspace"]
    now = datetime.now(timezone.utc)

    users = await _paged(ws, "/users")
    for u in users:
        gid = str(u.get("gid") or "")
        if not gid:
            continue
        row = (await session.execute(select(AsanaUserCache).where(
            AsanaUserCache.workspace_gid == ws, AsanaUserCache.asana_gid == gid))).scalar_one_or_none()
        email = str(u.get("email") or "").lower()
        name = str(u.get("name") or "")
        photo = ((u.get("photo") or {}).get("image_128x128") if isinstance(u.get("photo"), dict) else None)
        if row:
            row.email, row.name, row.photo_url, row.synced_at = email, name, photo, now
        else:
            session.add(AsanaUserCache(workspace_gid=ws, asana_gid=gid, email=email,
                                       name=name, photo_url=photo, synced_at=now))

    projects = await _paged(ws, "/projects")
    for p in projects:
        gid = str(p.get("gid") or "")
        if not gid:
            continue
        row = (await session.execute(select(AsanaProjectCache).where(
            AsanaProjectCache.project_gid == gid))).scalar_one_or_none()
        if row:
            row.name = str(p.get("name") or "")
            row.archived = bool(p.get("archived"))
            row.synced_at = now
        else:
            session.add(AsanaProjectCache(project_gid=gid, name=str(p.get("name") or ""),
                                          archived=bool(p.get("archived")), synced_at=now))
    await session.commit()
    return {"users": len(users), "projects": len(projects)}


async def list_projects(session: AsyncSession, *, ai_only: bool) -> list[dict]:
    q = select(AsanaProjectCache).order_by(AsanaProjectCache.name)
    if ai_only:
        q = q.where(AsanaProjectCache.ai_enabled.is_(True), AsanaProjectCache.archived.is_(False))
    rows = (await session.execute(q)).scalars().all()
    return [{"project_gid": r.project_gid, "name": r.name, "archived": r.archived,
             "ai_enabled": r.ai_enabled, "keywords": r.keywords, "ai_notes": r.ai_notes}
            for r in rows]


async def resolve_assignee(session: AsyncSession, assignee_user_id: str) -> dict:
    """Hub user → Asana gid. Manual mapping first, then unique lowercase email
    match against the cache; ambiguous/missing → clear error with candidates."""
    from ..models import User
    target = (await session.execute(select(User).where(User.id == assignee_user_id))).scalar_one_or_none()
    if not target:
        raise AsanaError("User Hub tidak ditemukan", 404)

    existing = (await session.execute(select(UserIntegration).where(
        UserIntegration.user_id == assignee_user_id, UserIntegration.provider == "asana"))).scalar_one_or_none()
    if existing:
        return {"gid": existing.external_id, "email": existing.provider_email or "",
                "name": existing.provider_name or "", "status": existing.status}

    email = str(target.email or "").lower()
    if not email:
        raise AsanaError(f"User {target.display_name or target.username} belum punya mapping Asana", 422)
    matches = (await session.execute(select(AsanaUserCache).where(
        AsanaUserCache.email == email))).scalars().all()
    if len(matches) == 1:
        m = matches[0]
        session.add(UserIntegration(user_id=assignee_user_id, provider="asana",
                                    external_id=m.asana_gid, provider_email=m.email,
                                    provider_name=m.name, status="auto"))
        return {"gid": m.asana_gid, "email": m.email, "name": m.name, "status": "auto"}
    candidates = [{"gid": m.asana_gid, "email": m.email, "name": m.name} for m in matches]
    if len(matches) > 1:
        raise AsanaError(f"Email {email} match {len(matches)} user Asana — set mapping manual. "
                         f"Kandidat: {json.dumps(candidates, ensure_ascii=False)}", 409)
    raise AsanaError(f"Email {email} tidak ada di cache Asana — jalankan sync atau set mapping manual", 422)


async def _validate_project(session: AsyncSession, project_gid: str, *, for_ai: bool) -> str:
    row = (await session.execute(select(AsanaProjectCache).where(
        AsanaProjectCache.project_gid == project_gid))).scalar_one_or_none()
    if not row:
        raise AsanaError("Project tidak ada di cache — jalankan sync dulu", 422)
    if row.archived:
        raise AsanaError("Project archived", 422)
    if for_ai and not row.ai_enabled:
        raise AsanaError("Project tidak aktif untuk AI (ai_enabled=false)", 422)
    return row.name


async def create_task(session: AsyncSession, actor, params: dict, *, for_ai: bool = False) -> dict:
    """Create an Asana task via Maton. Output is a short summary only."""
    cfg = _maton()
    data: dict = {"name": params["name"], "notes": params.get("notes") or "", "workspace": cfg["workspace"]}
    if params.get("project_gid"):
        data["projects"] = [params["project_gid"]]
        await _validate_project(session, params["project_gid"], for_ai=for_ai)
    assignee_info: dict = {}
    if params.get("assignee_user"):
        assignee_info = await resolve_assignee(session, params["assignee_user"])
        data["assignee"] = assignee_info["gid"]
    if params.get("due_on"):
        data["due_on"] = params["due_on"]

    payload = await _request("POST", "/tasks", json_body={"data": data})
    task = payload.get("data") or {}
    if not task.get("gid"):
        raise AsanaError("Respons Asana tidak valid (tanpa gid)")
    _audit(session, actor, "nekowork.asana.task_create", str(task["gid"]), {
        "name": params["name"], "project": params.get("project_gid") or "",
        "assignee": assignee_info.get("email") or "", "for_ai": for_ai})
    return {"gid": task.get("gid"), "name": task.get("name") or params["name"],
            "url": task.get("permalink_url") or "", "assignee": assignee_info.get("email") or "",
            "assignee_status": assignee_info.get("status") or ""}


async def set_task_completed(session: AsyncSession, actor, task_gid: str, completed: bool) -> dict:
    payload = await _request("PUT", f"/tasks/{task_gid}", json_body={"data": {"completed": completed}})
    task = payload.get("data") or {}
    _audit(session, actor, "nekowork.asana.task_complete", task_gid, {"completed": completed})
    return {"gid": task_gid, "name": task.get("name") or "", "completed": bool(task.get("completed")),
            "url": task.get("permalink_url") or ""}


async def list_identities(session: AsyncSession, user, *, own_only: bool) -> list[dict]:
    q = select(UserIntegration).order_by(UserIntegration.id)
    if own_only:
        q = q.where(UserIntegration.user_id == user.id)
    rows = (await session.execute(q)).scalars().all()
    return [{"id": r.id, "user_id": r.user_id, "provider": r.provider, "external_id": r.external_id,
             "provider_email": r.provider_email, "provider_name": r.provider_name, "status": r.status}
            for r in rows]


async def set_identity(session: AsyncSession, actor, *, user_id: str, external_id: str,
                       provider_email: str | None, provider_name: str | None,
                       status: str = "manual") -> dict:
    row = (await session.execute(select(UserIntegration).where(
        UserIntegration.user_id == user_id, UserIntegration.provider == "asana"))).scalar_one_or_none()
    if row:
        row.external_id, row.provider_email, row.provider_name, row.status = \
            external_id, provider_email, provider_name, status
    else:
        row = UserIntegration(user_id=user_id, provider="asana", external_id=external_id,
                              provider_email=provider_email, provider_name=provider_name, status=status)
        session.add(row)
    await session.flush()
    _audit(session, actor, "nekowork.asana.identity_set", str(row.id),
           {"user": user_id, "external_id": external_id, "status": status})
    return {"id": row.id, "user_id": user_id, "external_id": external_id, "status": status}


async def delete_identity(session: AsyncSession, actor, identity_id: int) -> bool:
    row = (await session.execute(select(UserIntegration).where(
        UserIntegration.id == identity_id))).scalar_one_or_none()
    if not row:
        return False
    await session.delete(row)
    _audit(session, actor, "nekowork.asana.identity_delete", str(identity_id), {"user": row.user_id})
    return True


def _audit(session: AsyncSession, actor, action: str, target_id: str, detail: dict) -> None:
    from ..models import AuditLog
    session.add(AuditLog(
        actor_id=actor.id,
        actor_name=actor.display_name or actor.username,
        actor_org_id=actor.org_id,
        action=action,
        target_type="nekowork_asana",
        target_id=str(target_id),
        target_label=detail.get("name") or action,
        detail=detail,
    ))
