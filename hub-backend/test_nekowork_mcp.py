"""Nekowork MCP endpoint: token auth, handshake, tools/list+call, rate limit."""
import importlib
import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models
from app.api import nekowork, nekowork_mcp
from app.auth import sign_token
from app.db import Base, get_session
from app.services import nekowork_mcp as svc

for model_file in (Path(__file__).parent / "app").glob("*models.py"):
    importlib.import_module("app." + model_file.stem)


class McpEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                models.User(id="u1", email="budi@x.com", username="budi", display_name="Budi"),
                models.User(id="adm", email="admin@x.com", username="admin", is_super=True),
            ])
            await session.commit()

        async def override_session():
            async with self.sessions() as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(nekowork_mcp.router)
        self.app.include_router(nekowork.router)
        self.app.dependency_overrides[get_session] = override_session
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

        async def fake_maton_request(method, path, *, params=None, json_body=None):
            if path == "/tasks" and method == "POST":
                return {"data": {"gid": "t9", "name": json_body["data"]["name"], "permalink_url": "http://a/t9"}}
            if "/tasks/search" in path:
                return {"data": [{"gid": "t1", "name": "Tugas Budi", "completed": False,
                                  "assignee": {"name": "Budi"}, "permalink_url": "http://a/t1",
                                  "due_on": None}]}
            raise AssertionError(f"unexpected {method} {path}")

        patcher = mock.patch.object(svc.nekowork_asana, "_request", fake_maton_request)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(svc.nekowork_asana, "_maton",
                                     return_value={"base": "x", "headers": {}, "workspace": "ws1"})
        patcher2.start()
        self.addCleanup(patcher2.stop)
        svc._WINDOW.clear()

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def _admin(self):
        token = sign_token("adm")
        return {"Authorization": f"Bearer {token}"}

    async def _mint(self):
        res = await self.http.post("/api/v1/nekowork/mcp-tokens", json={"name": "hermes-test"},
                                   headers=await self._admin())
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def _rpc(self, method, rid=1, **params):
        return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}

    async def test_401_without_token(self):
        res = await self.http.post("/mcp", json=self._rpc("initialize"))
        self.assertEqual(res.status_code, 401)

    async def test_401_with_garbage_token(self):
        res = await self.http.post("/mcp", json=self._rpc("initialize"),
                                   headers={"Authorization": "Bearer nwmcp-bogus"})
        self.assertEqual(res.status_code, 401)

    async def test_initialize_and_tools_list(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        res = await self.http.post("/mcp", json=self._rpc("initialize"), headers=auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["result"]["serverInfo"]["name"], "nekowork")
        res = await self.http.post("/mcp", json=self._rpc("tools/list"), headers=auth)
        tools = [t["name"] for t in res.json()["result"]["tools"]]
        self.assertEqual(tools, ["list_employees", "search_tasks", "set_identity",
                                 "delete_identity", "create_asana_task", "complete_asana_task"])

    async def test_disabled_token_401(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        res = await self.http.post("/mcp", json=self._rpc("initialize"), headers=auth)
        self.assertEqual(res.status_code, 200)
        res = await self.http.delete(f"/api/v1/nekowork/mcp-tokens/{minted['id']}", headers=await self._admin())
        self.assertTrue(res.json()["disabled"])
        res = await self.http.post("/mcp", json=self._rpc("initialize"), headers=auth)
        self.assertEqual(res.status_code, 401)

    async def test_rate_limit_429(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        body = self._rpc("tools/list")
        codes = set()
        for _ in range(svc.RATE_LIMIT + 1):
            codes.add((await self.http.post("/mcp", json=body, headers=auth)).status_code)
        self.assertIn(429, codes)
        self.assertEqual(len(svc._WINDOW[list(svc._WINDOW.keys())[0]]), svc.RATE_LIMIT)

    async def test_call_list_employees_and_search(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        res = await self.http.post("/mcp", json=self._rpc("tools/call", name="list_employees",
                                                          arguments={}), headers=auth)
        rows = res.json()["result"]["structuredContent"]["employees"]
        self.assertIn("budi@x.com", [r["email"] for r in rows])
        async with self.sessions() as session:
            session.add(svc.nekowork_asana.AsanaUserCache(workspace_gid="ws1", asana_gid="111",
                                                          email="budi@x.com", name="Budi"))
            await session.commit()
        res = await self.http.post("/mcp", json=self._rpc("tools/call", name="search_tasks",
                                                          arguments={"assignee": "budi@x.com",
                                                                     "completed": False}), headers=auth)
        tasks = res.json()["result"]["structuredContent"]["tasks"]
        self.assertEqual(tasks[0]["name"], "Tugas Budi")

    async def test_call_create_task_ai_gating_and_error_shape(self):
        async with self.sessions() as session:
            session.add(svc.nekowork_asana.AsanaProjectCache(project_gid="p-no", name="NonAI",
                                                             ai_enabled=False))
            await session.commit()
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        with mock.patch.object(svc.nekowork_asana, "_maton",
                               return_value={"base": "x", "headers": {}, "workspace": "ws1"}):
            res = await self.http.post("/mcp", json=self._rpc("tools/call", name="create_asana_task",
                                                              arguments={"name": "X", "project_gid": "p-no"}),
                                       headers=auth)
        body = res.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("ai_enabled", body["content"][0]["text"])
        # 422 shape, not HTTP 500 — protocol stays 200
        self.assertEqual(res.status_code, 200)

    async def test_set_and_delete_identity_via_mcp(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        res = await self.http.post("/mcp", json=self._rpc("tools/call", name="set_identity",
                                                          arguments={"user": "budi@x.com", "provider": "asana",
                                                                     "external_id": "777",
                                                                     "provider_name": "Budi A"}), headers=auth)
        self.assertEqual(res.json()["result"]["structuredContent"]["external_id"], "777")
        res = await self.http.post("/mcp", json=self._rpc("tools/call", name="delete_identity",
                                                          arguments={"user": "u1", "provider": "asana"}), headers=auth)
        self.assertTrue(res.json()["result"]["structuredContent"]["removed"])
        async with self.sessions() as session:
            rows = (await session.execute(select(svc.nekowork_asana.UserIntegration))).scalars().all()
            self.assertEqual(len(rows), 0)

    async def test_unknown_tool_is_error(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        res = await self.http.post("/mcp", json=self._rpc("tools/call", name="repo_activity",
                                                          arguments={}), headers=auth)
        self.assertTrue(res.json()["result"]["isError"])

    async def test_last_used_at_updated(self):
        minted = await self._mint()
        auth = {"Authorization": "Bearer " + minted["token"]}
        await self.http.post("/mcp", json=self._rpc("initialize"), headers=auth)
        async with self.sessions() as session:
            row = (await session.execute(select(svc.McpToken))).scalar_one()
            self.assertIsNotNone(row.last_used_at)


if __name__ == "__main__":
    unittest.main()
