"""Nekowork gateway API: self-service New API token management.

RBAC per PRD modul_gateway: member manages own token, super admin sees all;
plaintext keys are returned exactly once at generate/reset and never stored.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import current_user
from ..models import User
from ..services import nekowork_asana, nekowork_mcp, nekowork_newapi, nekowork_usage
from ..services.nekowork_newapi import NewApiError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/nekowork", tags=["nekowork"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyBody(StrictBody):
    pass


def _http_error(exc: NewApiError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _plaintext_response(result: dict) -> dict:
    """The one response in the token lifecycle that carries the plaintext key."""
    return {
        "token": result["plaintext_key"],
        "maskedKey": result["masked_key"],
        "tokenName": result["token_name"],
    }


@router.post("/tokens/me")
async def generate_own_token(_body: EmptyBody | None = None,
                             user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)) -> dict:
    try:
        result = await nekowork_newapi.generate_token(session, user)
        await session.commit()
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    return _plaintext_response(result)


@router.post("/tokens/me/reset")
async def reset_own_token(_body: EmptyBody | None = None,
                          user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)) -> dict:
    try:
        result = await nekowork_newapi.generate_token(session, user, reset=True)
        await session.commit()
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    return _plaintext_response(result)


@router.delete("/tokens/me")
async def revoke_own_token(user: User = Depends(current_user),
                           session: AsyncSession = Depends(get_session)) -> dict:
    try:
        account = await nekowork_newapi.revoke_token(session, user)
        await session.commit()
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    return {"revoked": True, "username": account.username}


@router.get("/tokens/me")
async def my_token_status(user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)) -> dict:
    try:
        return await nekowork_newapi.account_status(session, user)
    except NewApiError as exc:
        raise _http_error(exc) from exc


@router.get("/tokens")
async def list_tokens(user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    try:
        return {"items": await nekowork_newapi.list_all_accounts(session)}
    except NewApiError as exc:
        raise _http_error(exc) from exc


@router.get("/usage/me")
async def my_usage(force: bool = False,
                   user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    """Own balance + 14-day usage (console and client app, PRD modul_token_dashboard)."""
    try:
        status = await nekowork_newapi.account_status(session, user)
        usage = await nekowork_usage.usage_for(status.get("username") or "", force=force)
    except NewApiError as exc:
        raise _http_error(exc) from exc
    return {"token": status, "usage": usage}


@router.get("/usage")
async def all_usage(force: bool = False,
                    user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    try:
        return {"items": await nekowork_usage.usage_all(session, force=force)}
    except NewApiError as exc:
        raise _http_error(exc) from exc


class AsanaTaskBody(StrictBody):
    name: str
    notes: str | None = None
    assignee_user: str | None = None
    project_gid: str | None = None
    due_on: str | None = None
    for_ai: bool = False


class AsanaCompleteBody(StrictBody):
    completed: bool


class AsanaIdentityBody(StrictBody):
    user_id: str
    external_id: str
    provider_email: str | None = None
    provider_name: str | None = None
    status: str = "manual"


@router.get("/asana/projects")
async def asana_projects(ai_only: bool = False,
                         user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)) -> dict:
    """Member sees AI-enabled projects only; admin can list all (PRD §3)."""
    try:
        items = await nekowork_asana.list_projects(session, ai_only=ai_only or not user.is_super)
    except nekowork_asana.AsanaError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"items": items}


@router.post("/asana/tasks")
async def asana_create_task(body: AsanaTaskBody,
                            user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)) -> dict:
    try:
        result = await nekowork_asana.create_task(session, user, body.model_dump(),
                                                  for_ai=body.for_ai)
        await session.commit()
    except nekowork_asana.AsanaError as exc:
        await session.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return result


@router.put("/asana/tasks/{task_gid}")
async def asana_complete_task(task_gid: str, body: AsanaCompleteBody,
                              user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)) -> dict:
    try:
        result = await nekowork_asana.set_task_completed(session, user, task_gid, body.completed)
        await session.commit()
    except nekowork_asana.AsanaError as exc:
        await session.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return result


@router.post("/asana/sync")
async def asana_sync(user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    try:
        return await nekowork_asana.sync_cache(session)
    except nekowork_asana.AsanaError as exc:
        await session.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("/asana/identities")
async def asana_list_identities(user: User = Depends(current_user),
                                session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await nekowork_asana.list_identities(session, user, own_only=not user.is_super)}


@router.post("/asana/identities")
async def asana_set_identity(body: AsanaIdentityBody,
                             user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    try:
        result = await nekowork_asana.set_identity(session, user, **body.model_dump())
        await session.commit()
    except nekowork_asana.AsanaError as exc:
        await session.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return result


@router.delete("/asana/identities/{identity_id}")
async def asana_delete_identity(identity_id: int,
                                user: User = Depends(current_user),
                                session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    removed = await nekowork_asana.delete_identity(session, user, identity_id)
    await session.commit()
    return {"removed": removed}


class McpTokenBody(StrictBody):
    name: str


@router.post("/mcp-tokens")
async def mcp_create_token(body: McpTokenBody,
                           user: User = Depends(current_user),
                           session: AsyncSession = Depends(get_session)) -> dict:
    """Admin: mint a bearer token for an external MCP client (show-once)."""
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    try:
        result = await nekowork_mcp.create_token(session, name=body.name)
        await session.commit()
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    return result


@router.get("/mcp-tokens")
async def mcp_list_tokens(user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    return {"items": await nekowork_mcp.list_tokens(session)}


@router.delete("/mcp-tokens/{token_id}")
async def mcp_disable_token(token_id: int,
                            user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)) -> dict:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")
    disabled = await nekowork_mcp.disable_token(session, token_id)
    await session.commit()
    return {"disabled": disabled}
