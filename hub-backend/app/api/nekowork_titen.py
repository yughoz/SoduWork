"""Nekowork Titen admin/member management + transparent /mcp proxy."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..deps import current_user
from ..models import User
from ..services import nekowork_titen
from ..services.nekowork_newapi import NewApiError
from .nekowork import StrictBody, _http_error

router = APIRouter(prefix="/api/v1/nekowork")


def _admin(user: User) -> None:
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Hanya super admin")


class TitenEnsureBody(StrictBody):
    pass


class TitenKeyBody(StrictBody):
    user: str  # email atau user id


class TitenSharedBody(StrictBody):
    name: str


class TitenMemberBody(StrictBody):
    user: str


class TitenRotateBody(StrictBody):
    pass


@router.get("/titen/status")
async def titen_status(user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    return await nekowork_titen.status()


@router.post("/titen/ensure")
async def titen_ensure(user: User = Depends(current_user)) -> dict:
    _admin(user)
    try:
        return await nekowork_titen.ensure_server()
    except NewApiError as exc:
        raise _http_error(exc) from exc


async def _resolve_user(session: AsyncSession, ref: str) -> User:
    found = (await session.execute(
        select(User).where((User.id == ref) | (User.email == ref)))).scalars().first()
    if not found:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    return found


@router.post("/titen/keys")
async def titen_mint_key(body: TitenKeyBody, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    target = await _resolve_user(session, body.user)
    try:
        return await nekowork_titen.mint_user_key(session, target.id, username=target.username)
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@router.get("/titen/keys")
async def titen_list_keys(user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    from ..nekowork_models import TitenKey
    rows = (await session.execute(select(TitenKey).order_by(TitenKey.id))).scalars().all()
    return {"items": [{"id": r.id, "user_id": r.user_id, "key_id": r.key_id,
                       "status": r.status, "label": r.label} for r in rows]}


@router.delete("/titen/keys/{user_id}")
async def titen_revoke_key(user_id: str, user: User = Depends(current_user),
                           session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    try:
        return {"revoked": await nekowork_titen.revoke_user_key(session, user_id)}
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@router.get("/titen/me")
async def titen_me(user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    from ..nekowork_models import TitenKey
    rows = (await session.execute(
        select(TitenKey).where(TitenKey.user_id == user.id,
                               TitenKey.status == "active"))).scalars().all()
    return {"has_key": bool(rows), "key_id": rows[0].key_id if rows else None,
            "client_url": f"{get_settings().hub_public_url.rstrip('/') or 'http://127.0.0.1:18989'}/api/v1/titen/mcp"}


@router.post("/titen/me/rotate")
async def titen_rotate(user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)) -> dict:
    try:
        return await nekowork_titen.mint_user_key(session, user.id, username=user.username)
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@router.get("/titen/shared-projects")
async def titen_shared_list(user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)) -> dict:
    from ..nekowork_models import TitenSharedMember, TitenSharedProject
    rows = (await session.execute(select(TitenSharedProject).order_by(TitenSharedProject.id))).scalars().all()
    members = (await session.execute(select(TitenSharedMember))).scalars().all()
    by_project: dict[int, list[str]] = {}
    for m in members:
        by_project.setdefault(m.project_row_id, []).append(m.user_id)
    return {"items": [{"id": r.id, "name": r.name, "reference": r.reference,
                       "project_id": r.project_gid, "members": by_project.get(r.id, [])}
                      for r in rows]}


@router.post("/titen/shared-projects")
async def titen_shared_create(body: TitenSharedBody, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    try:
        row = await nekowork_titen.create_shared_project(session, body.name, user.id)
        return {"id": row.id, "project_id": row.project_gid, "name": row.name,
                "reference": row.reference}
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@router.post("/titen/shared-projects/{shared_id}/members")
async def titen_member_add(shared_id: int, body: TitenMemberBody,
                           user: User = Depends(current_user),
                           session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    from ..nekowork_models import TitenSharedProject
    shared = (await session.execute(
        select(TitenSharedProject).where(TitenSharedProject.id == shared_id))).scalar_one_or_none()
    if not shared:
        raise HTTPException(status_code=404, detail="Shared project tidak ditemukan")
    target = await _resolve_user(session, body.user)
    try:
        return await nekowork_titen.set_shared_member(session, shared, target.id, joined=True)
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@router.delete("/titen/shared-projects/{shared_id}/members/{member_user_id}")
async def titen_member_remove(shared_id: int, member_user_id: str,
                              user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)) -> dict:
    _admin(user)
    from ..nekowork_models import TitenSharedProject
    shared = (await session.execute(
        select(TitenSharedProject).where(TitenSharedProject.id == shared_id))).scalar_one_or_none()
    if not shared:
        raise HTTPException(status_code=404, detail="Shared project tidak ditemukan")
    try:
        return await nekowork_titen.set_shared_member(session, shared, member_user_id, joined=False)
    except NewApiError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


# Transparent proxy: Authorization header (titen_sk_…) forwarded as-is; Titen
# does its own auth. Hub is loopback-only, so no extra hub-level gate here.
async def _proxy(request: Request) -> Response:
    body = await request.body()
    headers = {}
    if request.headers.get("authorization"):
        headers["Authorization"] = request.headers["authorization"]
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    prefix = "/api/v1/nekowork/titen/mcp"
    target = "/mcp" + request.url.path[len(prefix):]
    try:
        async with httpx.AsyncClient(base_url=get_settings().titen_base_url, timeout=120) as client:
            upstream = await client.request(request.method, target, content=body,
                                            headers=headers, params=dict(request.query_params))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"titen upstream: {exc}") from exc
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))


@router.api_route("/titen/mcp", methods=["POST", "GET", "DELETE"])
async def titen_mcp(request: Request) -> Response:
    return await _proxy(request)


@router.api_route("/titen/mcp/{rest:path}", methods=["POST", "GET", "DELETE"])
async def titen_mcp_rest(rest: str, request: Request) -> Response:
    return await _proxy(request)
