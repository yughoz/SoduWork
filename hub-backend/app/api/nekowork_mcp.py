"""Nekowork /mcp endpoint: plain JSON-RPC over HTTP, same style as the built-in
task MCP. Auth (bearer mcp token) is checked before any protocol work.
"""
from __future__ import annotations

import logging
import types

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import nekowork_mcp
from ..services.nekowork_newapi import NewApiError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nekowork", "version": "1"}

router = APIRouter(tags=["nekowork-mcp"])


def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _fail(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


@router.post("/mcp")
@router.post("/mcp/")
async def rpc(request: Request,
              body: dict = Body(default_factory=dict),
              authorization: str | None = Header(default=None),
              session: AsyncSession = Depends(get_session)) -> dict:
    try:
        token = await nekowork_mcp.authenticate(session, authorization)
        await session.commit()   # persist last_used_at + rate limit side effects
    except NewApiError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    if token is None:
        raise HTTPException(status_code=401, detail="MCP token tidak valid")

    rid = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}

    if method == "initialize":
        return _ok(rid, {"protocolVersion": PROTOCOL_VERSION,
                         "capabilities": {"tools": {}},
                         "serverInfo": SERVER_INFO})
    if method.startswith("notifications/"):
        return {"jsonrpc": "2.0"}
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": nekowork_mcp.TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        actor = nekowork_mcp.mcp_actor(token)
        try:
            data = await _call_tool(name, args, session, actor)
            await session.commit()
        except NewApiError as exc:
            await session.rollback()
            return _ok(rid, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            log.warning("MCP tool %s gagal: %s", name, e)
            return _ok(rid, {"content": [{"type": "text", "text": f"Tool gagal: {e}"}],
                             "isError": True})
        return _ok(rid, {"content": [{"type": "text", "text": _as_text(data)}],
                         "structuredContent": data if isinstance(data, dict) else None})
    return _fail(rid, -32601, f"Method tidak dikenal: {method}")


async def _call_tool(name: str, args: dict, session: AsyncSession, actor: types.SimpleNamespace):
    if name == "list_employees":
        return {"employees": await nekowork_mcp.list_employees(session, str(args.get("provider") or "asana"))}
    if name == "search_tasks":
        return {"tasks": await nekowork_mcp.search_tasks(
            session, query=(str(args["query"]) if args.get("query") else None),
            assignee=(str(args["assignee"]) if args.get("assignee") else None),
            completed=(bool(args["completed"]) if "completed" in args else None))}
    if name == "set_identity":
        return await nekowork_mcp.set_identity(session, actor, **{
            "user": str(args.get("user") or ""), "provider": str(args.get("provider") or "asana"),
            "external_id": str(args.get("external_id") or ""),
            "provider_email": (str(args["provider_email"]) if args.get("provider_email") else None),
            "provider_name": (str(args["provider_name"]) if args.get("provider_name") else None)})
    if name == "delete_identity":
        return await nekowork_mcp.delete_identity(
            session, actor, user=str(args.get("user") or ""),
            provider=str(args.get("provider") or "asana"))
    if name == "create_asana_task":
        return await nekowork_mcp.create_asana_task(session, actor, args)
    if name == "complete_asana_task":
        return await nekowork_mcp.complete_asana_task(session, actor, args)
    raise NewApiError(f"Tool tidak dikenal: {name}", 404)


def _as_text(data) -> str:
    import json
    if isinstance(data, dict) and "employees" in data:
        rows = data["employees"]
        return "\n".join(f"- {r['name']} ({r['email'] or '-'}) asana={r['external_id'] or '-'}" for r in rows) or "kosong"
    if isinstance(data, dict) and "tasks" in data:
        rows = data["tasks"]
        return "\n".join(f"- [{r['gid']}] {r['name']} ({'done' if r['completed'] else 'open'}) {r.get('assignee') or ''}" for r in rows) or "kosong"
    return json.dumps(data, ensure_ascii=False, default=str)
