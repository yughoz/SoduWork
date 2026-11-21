"""Nekowork web client: password login for browsers and remote permission answers.

The browser is not a pre-registered device. Login verifies the account the same
way as the desktop password flow, then binds a stable web-only Client row to the
user and issues a normal device session token. Every other web operation reuses
the existing desktop endpoints with that token, so quota, scoping and audit
behavior cannot drift between surfaces.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import current_client
from ..models import Client, User
from ..services import client_access, device_auth
from ..services.client_access import ClientAccessError

router = APIRouter(prefix="/api/v1/web", tags=["web-client"])

_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_ATTEMPTS = 5
_attempts: dict[str, tuple[int, float]] = {}


def _throttle(key: str) -> None:
    now = time.monotonic()
    count, since = _attempts.get(key, (0, now))
    if now - since > _LOGIN_WINDOW_SECONDS:
        count, since = 0, now
    count += 1
    _attempts[key] = (count, since)
    if count > _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, "Terlalu banyak percobaan login, coba lagi nanti")


def _web_device_id(user_id: str) -> str:
    return "web-" + hashlib.sha256(("web-client:" + user_id).encode()).hexdigest()[:16]


async def _login_user(session: AsyncSession, account: str, password: str) -> User:
    normalized = client_access.normalize_account(account)
    user = await client_access.find_login_user(session, normalized)
    if user is None or not user.is_active or user.account_deleted_at:
        raise HTTPException(401, "Akun tidak ditemukan atau dinonaktifkan")
    from ..auth import verify_password
    if not verify_password(password, user.password_hash):
        raise HTTPException(401, "Akun atau kata sandi salah")
    return user


@router.post("/login")
async def web_login(
    body: dict = Body(default_factory=dict),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
):
    """Password login for the browser. Mints a web device binding and a session token."""
    account = str(body.get("account") or "").strip()
    password = str(body.get("password") or "")
    if not account or not password:
        raise HTTPException(400, "Akun dan kata sandi wajib diisi")
    peer = request.client.host if request is not None and request.client else ""
    _throttle((peer or "") + ":" + account.lower())
    user = await _login_user(session, account, password)

    device_id = _web_device_id(user.id)
    client = await session.get(Client, device_id)
    if client is None:
        client = Client(id=device_id, name="Web · " + (user.display_name or user.username)[:40],
                        secret_hash="", platform="web", app_version="web-1")
        session.add(client)
    # Rotate the device secret on every login; only the session token ever leaves the server.
    client.secret_hash = device_auth.hash_secret(secrets.token_urlsafe(24))
    client.platform = "web"
    client.app_version = "web-1"
    device_auth.activate(client, user)
    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(client)

    token, expires_in = device_auth.issue_session(client)
    return {"ok": True, "token": token, "expiresIn": expires_in,
            "device": client.id,
            "user": {"username": user.username, "displayName": user.display_name or user.username}}


_DECISIONS = {"once", "session", "deny"}


@router.post("/tasks/{tid}/permission")
async def web_task_permission(
    tid: str,
    body: dict = Body(default_factory=dict),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(get_session),
):
    """Answer a runner permission request for a task this account owns.

    Forwards to the executing runner through the hub websocket, mirroring the
    desktop's local job.permission answer path. Remote conversations otherwise
    have no interactive approval surface outside the desktop app.
    """
    from ..services import ai_remote, runner_hub, task_access, task_events

    request_id = str(body.get("requestId") or "")
    decision = str(body.get("decision") or "")
    revision = body.get("policyRevision")
    option_id = body.get("optionId")
    if decision not in _DECISIONS:
        raise HTTPException(400, "Keputusan permission tidak valid")
    if not request_id or not isinstance(revision, int):
        raise HTTPException(400, "requestId dan policyRevision wajib")
    await task_access.view_task(session, client, tid)
    from .tasks import _dispatch_ref
    ref = await _dispatch_ref(session, tid)
    if not ref:
        raise HTTPException(409, "Tugas ini belum dieksekusi")
    runner = ai_remote._runner_of_ref(ref)
    if runner is None:
        raise HTTPException(409, "PC eksekusi sedang offline; sambungkan desktop client lalu coba lagi")
    payload: dict = {"ref": ref, "requestId": request_id, "decision": decision,
                     "policyRevision": revision}
    if decision == "answer":
        if not isinstance(option_id, str) or not option_id:
            raise HTTPException(400, "Pertanyaan pilihan membutuhkan optionId")
        payload["optionId"] = option_id
    res = runner_hub.call_sync(runner, "job.permission", payload, timeout=30)
    data = res.get("data") or {}
    if not res.get("ok") or data.get("ok") is False:
        detail = str(data.get("error") or res.get("error") or "Jawaban permission ditolak runner")
        raise HTTPException(409, detail)
    from .tasks import _actor_label, _actor_user
    actor = await _actor_user(session, client)
    task_events.record(session, tid, "web.permission",
                       {"ref": ref, "requestId": request_id, "decision": decision},
                       actor=_actor_label(actor, client))
    await session.commit()
    return {"ok": True}
