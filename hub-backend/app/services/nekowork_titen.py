"""Embedded Titen memory server (PRD 4b): lifecycle, per-user fenced keys,
shared projects, client auto-setup. Secrets live in a 0600 meta file, never in DB."""
import asyncio
import hashlib
import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Client
from ..nekowork_models import TitenKey, TitenSharedMember, TitenSharedProject
from .nekowork_newapi import NewApiError

KERNEL_SCOPES = ["projects:resolve", "observations:write", "claims:write",
                 "context:compile", "feedback:write", "evidence:read"]
DB_NAME = "titen.db"
META_NAME = "titen-meta.json"
_MCP_ENTRY = "nekowork-titen"

_lock = asyncio.Lock()
_proc: subprocess.Popen | None = None


def _db_path() -> Path:
    return Path.cwd() / DB_NAME


def _meta_path() -> Path:
    return Path.cwd() / META_NAME


def _meta() -> dict:
    try:
        return json.loads(_meta_path().read_text())
    except FileNotFoundError:
        return {}


def _bin() -> str:
    return get_settings().titen_bin or shutil.which("titen") or str(Path.home() / ".bun/bin/titen")


def _run(args: list[str]) -> str:
    proc = subprocess.run([_bin()] + args, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise NewApiError(f"titen CLI gagal: {proc.stderr.strip()[:200]}", status=500)
    return proc.stdout


async def _api(method: str, path: str, token: str | None = None, json_body=None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=get_settings().titen_base_url, timeout=30) as client:
        resp = await client.request(method, path, json=json_body, headers=headers)
    if resp.status_code >= 400:
        try:
            msg = resp.json()["error"]["message"]
        except Exception:
            msg = resp.text[:200]
        raise NewApiError(resp.status_code, f"titen: {msg}")
    return resp.json().get("data", {})


async def _healthy() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(get_settings().titen_base_url + "/healthz", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


async def ensure_server() -> dict:
    """Bootstrap (first run) + spawn `titen serve` as a child process. Idempotent."""
    global _proc
    async with _lock:
        if await _healthy():
            return {"running": True, "spawned": False, **_meta_meta()}
        db = _db_path()
        if not db.exists():
            out = _run(["bootstrap", "--db", str(db), "--org", "Nekowork",
                        "--username", "owner", "--label", "owner"])
            m_org = re.search(r"organization: (org_[0-9a-f]+)", out)
            m_pw = re.search(r"temporary_password: (\S+)", out)
            if not (m_org and m_pw):
                raise NewApiError("bootstrap: output tidak bisa di-parse", status=500)
            org_id, temp_pw = m_org.group(1), m_pw.group(1)
            # Staged dashboard session -> set a real owner password -> full session -> hub service key.
            staged = (await _api("POST", "/v1/dashboard-sessions",
                                 json_body={"username": "owner", "password": temp_pw}))["api_key"]
            owner_pw = secrets.token_urlsafe(24)
            await _api("PATCH", "/v1/operator-accounts/current/password",
                       token=staged, json_body={"password": owner_pw})
            full = (await _api("POST", "/v1/dashboard-sessions",
                               json_body={"username": "owner", "password": owner_pw}))["api_key"]
            hub = await _api("POST", "/v1/keys", token=full,
                             json_body={"label": "nekowork-hub", "kind": "service",
                                        "scopes": ["*"], "max_trust": "policy_approved"})
            meta = {"org_id": org_id, "owner_username": "owner", "owner_password": owner_pw,
                    "hub_key": hub["api_key"], "hub_key_id": hub["key_id"]}
            _meta_path().write_text(json.dumps(meta, indent=1))
            _meta_path().chmod(0o600)
        meta = _meta()
        _proc = subprocess.Popen(
            [_bin(), "serve", "--db", str(db), "--port", str(get_settings().titen_port), "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await _healthy():
                break
        else:
            raise NewApiError("titen serve tidak kunjung healthy", status=502)
        return {"running": True, "spawned": True, **_meta_meta()}


def _meta_meta() -> dict:
    meta = _meta()
    return {"org_id": meta.get("org_id", ""), "db": str(_db_path())}


async def _hub_key() -> str:
    meta = _meta()
    if not meta.get("hub_key"):
        raise NewApiError("Titen belum di-bootstrap (panggil /titen/ensure)", status=503)
    return meta["hub_key"]


async def _ensure_project(reference: str, default_visibility: str | None = None) -> str:
    data = await _api("POST", "/v1/projects/resolve", token=await _hub_key(),
                      json_body={"reference": reference, "create": True})
    pid = data.get("project_id") or data.get("id") or ""
    if not pid:
        raise NewApiError(f"projects/resolve tidak balikin project id: {list(data)}", status=500)
    if default_visibility:
        await _api("PATCH", f"/v1/projects/{pid}", token=await _hub_key(),
                   json_body={"default_visibility": default_visibility})
    return pid


def _client_entry(raw: str) -> dict:
    base = (get_settings().hub_public_url or f"http://127.0.0.1:{getattr(get_settings(), 'port', 18989)}").rstrip("/")
    return {"name": _MCP_ENTRY, "transport": "http",
            "url": f"{base}/api/v1/titen/mcp",
            "headers": {"Authorization": f"Bearer {raw}"}}


async def _setup_clients(session: AsyncSession, user_id: str, raw: str) -> int:
    entry = _client_entry(raw)
    clients = (await session.execute(
        select(Client).where(Client.user_ref == user_id))).scalars().all()
    for device in clients:
        servers = [s for s in (device.mcp_servers or []) if s.get("name") != _MCP_ENTRY]
        servers.append(entry)
        device.mcp_servers = servers
    return len(clients)


async def _clear_clients(session: AsyncSession, user_id: str) -> None:
    for device in (await session.execute(
            select(Client).where(Client.user_ref == user_id))).scalars().all():
        device.mcp_servers = [s for s in (device.mcp_servers or []) if s.get("name") != _MCP_ENTRY]


async def _mint_raw(label: str, write_projects: list[str]) -> tuple[str, str]:
    body = {"label": label, "kind": "agent", "scopes": KERNEL_SCOPES, "max_trust": "asserted"}
    if write_projects:
        body["write_projects"] = write_projects
    data = await _api("POST", "/v1/keys", token=await _hub_key(), json_body=body)
    return data["api_key"], data["key_id"]


async def mint_user_key(session: AsyncSession, user_id: str,
                        username: str = "", extra_projects: list[str] | None = None) -> dict:
    """Rotate-safe mint: revoke old keys, fence writes to the user's own project
    (+ shared projects), store hash only, auto-setup Client.mcp_servers."""
    await ensure_server()
    own = await _ensure_project(f"nk/{user_id}")
    raw, key_id = await _mint_raw(f"user:{user_id}", [own] + list(extra_projects or []))
    hub = await _hub_key()
    old = (await session.execute(
        select(TitenKey).where(TitenKey.user_id == user_id, TitenKey.status == "active"))).scalars().all()
    for row in old:
        try:
            await _api("DELETE", f"/v1/keys/{row.key_id}", token=hub)
        except NewApiError:
            pass  # already gone server-side; keep local bookkeeping
        row.status = "revoked"
        if row.rotated_at is None:
            from datetime import datetime, timezone
            row.rotated_at = datetime.now(timezone.utc)
    session.add(TitenKey(user_id=user_id, key_id=key_id,
                         key_hash=hashlib.sha256(raw.encode()).hexdigest(),
                         label=f"user:{user_id}", status="active"))
    devices = await _setup_clients(session, user_id, raw)
    await session.commit()
    return {"token": raw, "key_id": key_id, "project_id": own, "devices_updated": devices,
            "write_projects": [own] + list(extra_projects or [])}


async def revoke_user_key(session: AsyncSession, user_id: str) -> int:
    hub = await _hub_key()
    rows = (await session.execute(
        select(TitenKey).where(TitenKey.user_id == user_id, TitenKey.status == "active"))).scalars().all()
    for row in rows:
        try:
            await _api("DELETE", f"/v1/keys/{row.key_id}", token=hub)
        except NewApiError:
            pass
        row.status = "revoked"
    await _clear_clients(session, user_id)
    await session.commit()
    return len(rows)


async def create_shared_project(session: AsyncSession, name: str, admin_id: str) -> dict:
    await ensure_server()
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "shared"
    reference = f"shared/{slug}"
    pid = await _ensure_project(reference, default_visibility="organization")
    row = TitenSharedProject(project_gid=pid, name=name, reference=reference, created_by=admin_id)
    session.add(row)
    await session.commit()
    return row


async def _user_shared_project_ids(session: AsyncSession, user_id: str) -> list[str]:
    rows = (await session.execute(
        select(TitenSharedProject)
        .join(TitenSharedMember, TitenSharedMember.project_row_id == TitenSharedProject.id)
        .where(TitenSharedMember.user_id == user_id))).scalars().all()
    return [r.project_gid for r in rows]


async def set_shared_member(session: AsyncSession, shared: TitenSharedProject,
                            user_id: str, joined: bool) -> dict:
    """Membership change rotates the member's key (revoke + mint) per PRD."""
    existing = (await session.execute(
        select(TitenSharedMember).where(TitenSharedMember.project_row_id == shared.id,
                                        TitenSharedMember.user_id == user_id))).scalar_one_or_none()
    if joined and not existing:
        session.add(TitenSharedMember(project_row_id=shared.id, user_id=user_id))
    elif not joined and existing:
        await session.delete(existing)
    await session.commit()
    extras = await _user_shared_project_ids(session, user_id)
    result = await mint_user_key(session, user_id, extra_projects=extras)
    result["joined"] = joined
    return result


async def status() -> dict:
    import sqlalchemy as sa  # local: async engine only needed here
    meta = _meta()
    return {"running": await _healthy(), "db": str(_db_path()), "db_exists": _db_path().exists(),
            "org_id": meta.get("org_id", ""), "hub_key_id": meta.get("hub_key_id", ""),
            "meta_path": str(_meta_path()), "base_url": get_settings().titen_base_url}
