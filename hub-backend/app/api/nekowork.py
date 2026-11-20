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
from ..services import nekowork_newapi, nekowork_usage
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
