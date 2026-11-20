"""Nekowork New API gateway service (llm.nekotekno.xyz).

Ported from nekorporate-v2 ``src/lib/newapi.ts`` (M5-era contract):
management calls use the admin access token header pair, user provisioning is
deterministic, token plaintext is fetched once at creation and never stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..nekowork_models import NewApiAccount

log = logging.getLogger(__name__)

QUOTA_PER_UNIT = 500_000  # New API quota -> USD


class NewApiError(RuntimeError):
    """Gateway misconfiguration or upstream failure surfaced to the API layer."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def quota_to_usd(quota: int | None) -> float | None:
    if quota is None:
        return None
    return round(float(quota or 0) / QUOTA_PER_UNIT, 6)


def mask_token_key(raw_key: str) -> str:
    if not raw_key:
        return "****"
    if len(raw_key) <= 8:
        return f"{raw_key[:2]}****{raw_key[-2:]}"
    return f"{raw_key[:4]}****{raw_key[-4:]}"


def normalize_token_key(raw_key: str) -> str:
    key = str(raw_key or "").strip()
    if not key:
        return ""
    return key if key.startswith("sk-") else f"sk-{key}"


def gateway_username(user_id: str) -> str:
    """Same deterministic format as Nekorp (``nk-{userId}``, <=20 chars) so
    accounts provisioned by Nekorp resolve to the same gateway user."""
    return f"nk-{user_id}"[:20]


def gateway_password(user_id: str, secret: str) -> str:
    material = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).digest()
    return re.sub(r"[^a-zA-Z0-9]", "", base64.b64encode(material).decode())[:20]


def token_name_for(email: str | None, username: str | None = None) -> str:
    return f"nekowork-{email or username or 'user'}"


def legacy_token_names(email: str | None, user_id: str, username: str | None = None) -> list[str]:
    """Names Nekorp used; looked up first so old tokens are adopted, not duplicated."""
    names = []
    if email:
        names.append(f"nekorp-{email}")
    names.append(f"nekowork-{email or username or 'user'}")
    names.append("nekowork-user")  # pre-0.1 generic fallback
    names.append(f"nekorp-{user_id}")
    return names


@dataclass
class _Gateway:
    base_url: str
    headers: dict[str, str]

    @property
    def configured(self) -> bool:
        return bool(self.headers)


def _gateway() -> _Gateway:
    s = get_settings()
    token = s.newapi_admin_access_token.strip()
    uid = str(s.newapi_admin_access_user_id or "").strip()
    if not token or not uid:
        raise NewApiError(
            "New API admin access token belum diset (NEWAPI_ADMIN_ACCESS_TOKEN / NEWAPI_ADMIN_ACCESS_USER_ID)",
            status=503,
        )
    return _Gateway(
        base_url=s.newapi_base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {token}",
            "New-Api-User": uid,
            "Content-Type": "application/json",
        },
    )


async def _request(gw: _Gateway, method: str, path: str, *, params: dict | None = None,
                   json_body: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(method, gw.base_url + path, params=params,
                                        json=json_body, headers=gw.headers)
    except httpx.HTTPError as exc:
        raise NewApiError(f"New API tidak terjangkau: {exc}") from exc
    if resp.status_code >= 400:
        raise NewApiError(f"New API balas {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise NewApiError("New API balas bukan JSON") from exc


def _items(payload) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        data = data.get("items")
    return data if isinstance(data, list) else []


async def find_gateway_user(gw: _Gateway, username: str) -> dict | None:
    payload = await _request(gw, "GET", "/api/user/", params={"p": 0, "page_size": 500})
    for item in _items(payload):
        if item.get("username") == username:
            return item
    return None


async def create_gateway_user(gw: _Gateway, *, username: str, display_name: str,
                              password: str, group: str) -> dict:
    await _request(gw, "POST", "/api/user/", json_body={
        "username": username,
        "display_name": display_name[:20],
        "password": password,
        "group": group,
        "role": 1,
    })
    user = await find_gateway_user(gw, username)
    if not user:
        raise NewApiError(f"New API user {username} dibuat tapi tidak ditemukan")
    return user


async def list_tokens(gw: _Gateway) -> list[dict]:
    payload = await _request(gw, "GET", "/api/token/", params={"p": 0, "page_size": 200})
    return _items(payload)


async def find_token_by_name(gw: _Gateway, names: list[str]) -> dict | None:
    for token in await list_tokens(gw):
        if token.get("name") in names:
            return token
    return None


async def create_token(gw: _Gateway, *, name: str, group: str) -> dict:
    await _request(gw, "POST", "/api/token/", json_body={
        "name": name,
        "group": group,
        "remain_quota": 0,
        "unlimited_quota": True,
        "expired_time": -1,
        "model_limits_enabled": False,
        "model_limits": "",
        "allow_ips": "",
    })
    token = await find_token_by_name(gw, [name])
    if not token:
        raise NewApiError(f"Token {name} dibuat tapi tidak ditemukan lagi")
    return token


async def fetch_full_key(gw: _Gateway, token_id: int) -> str:
    payload = await _request(gw, "POST", f"/api/token/{token_id}/key")
    key = normalize_token_key((payload.get("data") or {}).get("key", ""))
    if not key:
        raise NewApiError(f"Token {token_id} tidak mengembalikan plaintext key")
    return key


async def delete_token(gw: _Gateway, token_id: int) -> None:
    await _request(gw, "DELETE", f"/api/token/{token_id}")


# ─── Account orchestration ────────────────────────────────────────────

async def get_or_create_account(session: AsyncSession, user) -> NewApiAccount:
    """Provision the gateway user on first use; idempotent."""
    account = (await session.execute(
        select(NewApiAccount).where(NewApiAccount.user_id == user.id))).scalar_one_or_none()
    if account:
        return account
    s = get_settings()
    gw = _gateway()
    username = gateway_username(user.id)
    remote = await find_gateway_user(gw, username)
    if not remote:
        secret = s.newapi_user_password_secret or s.auth_secret
        if not secret:
            raise NewApiError("NEWAPI_USER_PASSWORD_SECRET belum diset", status=503)
        remote = await create_gateway_user(
            gw, username=username,
            display_name=user.display_name or user.username,
            password=gateway_password(user.id, secret),
            group=s.newapi_token_group)
    account = NewApiAccount(
        user_id=user.id, newapi_user_id=int(remote["id"]), username=username)
    session.add(account)
    await session.flush()
    return account


async def generate_token(session: AsyncSession, user, *, reset: bool = False) -> dict:
    """Create a fresh token (after deleting any previous one when resetting)."""
    s = get_settings()
    gw = _gateway()
    account = await get_or_create_account(session, user)
    old = await find_token_by_name(gw, legacy_token_names(user.email, user.id, user.username))
    if reset and old:
        await delete_token(gw, int(old["id"]))
    elif not reset and account.token_id and old and int(old["id"]) == account.token_id:
        raise NewApiError("Token masih aktif; pakai reset untuk ganti", status=409)
    name = token_name_for(user.email, user.username)
    token = await create_token(gw, name=name, group=s.newapi_token_group)
    full_key = await fetch_full_key(gw, int(token["id"]))
    account.token_id = int(token["id"])
    account.token_name = name
    account.token_key_masked = mask_token_key(full_key)
    account.token_enabled = int(token.get("status") or 1) == 1
    await audit_account(session, user, "nekowork.token.reset" if reset else "nekowork.token.generate",
                        account, {"token_name": name})
    return {"plaintext_key": full_key, "masked_key": account.token_key_masked,
            "token_name": name, "account": account,
            "unlimited": bool(token.get("unlimited_quota"))}


async def revoke_token(session: AsyncSession, user) -> NewApiAccount:
    gw = _gateway()
    account = (await session.execute(
        select(NewApiAccount).where(NewApiAccount.user_id == user.id))).scalar_one_or_none()
    if not account or not account.token_id:
        raise NewApiError("Belum ada token aktif", status=404)
    await delete_token(gw, account.token_id)
    account.token_id = None
    account.token_name = None
    account.token_key_masked = None
    account.token_enabled = False
    await audit_account(session, user, "nekowork.token.revoke", account, {})
    return account


async def account_status(session: AsyncSession, user) -> dict:
    """Masked token state plus live remaining balance in USD."""
    account = (await session.execute(
        select(NewApiAccount).where(NewApiAccount.user_id == user.id))).scalar_one_or_none()
    if not account:
        return {"provisioned": False, "has_token": False}
    result: dict = {
        "provisioned": True,
        "username": account.username,
        "has_token": bool(account.token_id),
        "token_name": account.token_name,
        "masked_key": account.token_key_masked,
        "token_enabled": account.token_enabled,
    }
    if account.token_id:
        gw = _gateway()
        tokens = {int(t["id"]): t for t in await list_tokens(gw)}
        token = tokens.get(account.token_id)
        if token:
            result["balance_usd"] = quota_to_usd(token.get("remain_quota"))
            result["used_usd"] = quota_to_usd(token.get("used_quota"))
            result["unlimited"] = bool(token.get("unlimited_quota"))
            result["token_enabled"] = int(token.get("status") or 1) == 1
            account.token_enabled = result["token_enabled"]
        else:
            result["balance_usd"] = None
            result["token_enabled"] = False
    return result


async def list_all_accounts(session: AsyncSession) -> list[dict]:
    """Admin view: every mapping with live balance (no plaintext, ever)."""
    from ..models import User  # local import to avoid cycle at module load
    gw = _gateway()
    tokens = {str(t.get("name")): t for t in await list_tokens(gw)}
    rows = (await session.execute(
        select(NewApiAccount, User).join(User, User.id == NewApiAccount.user_id))).all()
    out = []
    for account, user in rows:
        token = tokens.get(account.token_name or "")
        out.append({
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "username": account.username,
            "token_name": account.token_name,
            "token_enabled": account.token_enabled and bool(token),
            "balance_usd": quota_to_usd(token.get("remain_quota")) if token else None,
        })
    return out


async def audit_account(session: AsyncSession, user, action: str,
                        account: NewApiAccount, detail: dict) -> None:
    from ..models import AuditLog
    session.add(AuditLog(
        actor_id=user.id,
        actor_name=user.display_name or user.username,
        actor_org_id=user.org_id,
        action=action,
        target_type="nekowork_newapi_account",
        target_id=account.id,
        target_label=account.token_name or account.username,
        detail=detail,
    ))
