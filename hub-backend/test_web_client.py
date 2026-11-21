"""Web client login and permission answer endpoints.

Isolated SQLite; runner transport is mocked. No real runner, hub websocket or
desktop client is contacted.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_root = tempfile.TemporaryDirectory(prefix="soduwork-web-client-")
os.environ.update(DATABASE_URL="sqlite+aiosqlite:///" + (Path(_root.name) / ".smoke.db").as_posix(),
                  SODUWORK_DATA_ROOT=str(Path(_root.name) / "data"),
                  AUTH_SECRET="isolated-web-client-signing-key", ADMIN_PASS="isolated-admin-password",
                  CLUSTER_REDIS_URL="", DEVICE_AUTH_COMPAT="false", SODUWORK_ENV="development")

import httpx
from fastapi import FastAPI
from app import models
from app.auth import hash_password
from app.db import Base, SessionLocal, engine
from app.api import tasks, web_client
from app.services import device_auth

app = FastAPI()
app.include_router(web_client.router)
app.include_router(tasks.router)

PASSWORD = "Web-client-password-1"


class WebClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        web_client._attempts.clear()
        async with SessionLocal() as session:
            session.add(models.User(id="alice", username="alice@example.test",
                                    display_name="Alice", org_id="company",
                                    password_hash=hash_password(PASSWORD), is_active=True))
            await session.commit()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://isolated.test")

    async def asyncTearDown(self):
        await self.http.aclose()

    async def login(self, account="alice@example.test", password=PASSWORD):
        return await self.http.post("/api/v1/web/login",
                                    json={"account": account, "password": password})

    async def test_login_ok_and_stable_device(self):
        first = await self.login()
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["user"]["username"], "alice@example.test")
        async with SessionLocal() as session:
            client = await session.get(models.Client, body["device"])
            self.assertIsNotNone(client)
            self.assertEqual(client.auth_state, "active")
            self.assertEqual(client.platform, "web")
            self.assertEqual(client.user_ref, "alice")
        second = await self.login()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["device"], body["device"])

    async def test_login_rejects_bad_password(self):
        response = await self.login(password="wrong-password")
        self.assertEqual(response.status_code, 401, response.text)

    async def test_login_rate_limited(self):
        for _ in range(web_client._LOGIN_MAX_ATTEMPTS):
            await self.login(password="wrong-password")
        self.assertEqual((await self.login()).status_code, 429)

    async def _token_headers(self):
        response = await self.login()
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Device " + response.json()["token"]}

    async def _seed_task(self):
        async with SessionLocal() as session:
            client = await session.get(models.Client, web_client._web_device_id("alice"))
            session.add(models.Task(id="t1", title="T", owner_client_id=client.id,
                                    assignee_user_id="alice"))
            await session.commit()

    async def test_permission_forwarded_to_runner(self):
        headers = await self._token_headers()
        await self._seed_task()
        fake_runner = SimpleNamespace(id="runner-1")
        forwarded = {}

        def fake_call_sync(runner, op, payload, timeout=30):
            forwarded.update(runner=runner.id, op=op, payload=payload)
            return {"ok": True, "data": {"ok": True}}

        fake_event = SimpleNamespace(detail={"ref": "ref-1"})
        with patch("app.services.ai_remote._runner_of_ref", return_value=fake_runner), \
             patch("app.services.runner_hub.call_sync", side_effect=fake_call_sync), \
             patch("app.services.ai_exec.last_dispatch_event", AsyncMock(return_value=fake_event)):
            response = await self.http.post("/api/v1/web/tasks/t1/permission", headers=headers,
                json={"requestId": "req-1", "decision": "once", "policyRevision": 3})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(forwarded["runner"], "runner-1")
        self.assertEqual(forwarded["op"], "job.permission")
        self.assertEqual(forwarded["payload"]["ref"], "ref-1")
        self.assertEqual(forwarded["payload"]["requestId"], "req-1")
        self.assertEqual(forwarded["payload"]["decision"], "once")
        self.assertEqual(forwarded["payload"]["policyRevision"], 3)

    async def test_permission_requires_valid_decision(self):
        headers = await self._token_headers()
        response = await self.http.post("/api/v1/web/tasks/t1/permission", headers=headers,
            json={"requestId": "req-1", "decision": "always", "policyRevision": 1})
        self.assertEqual(response.status_code, 400, response.text)

    async def test_permission_offline_runner(self):
        headers = await self._token_headers()
        await self._seed_task()
        fake_event = SimpleNamespace(detail={"ref": "ref-1"})
        with patch("app.services.ai_remote._runner_of_ref", return_value=None), \
             patch("app.services.ai_exec.last_dispatch_event", AsyncMock(return_value=fake_event)):
            response = await self.http.post("/api/v1/web/tasks/t1/permission", headers=headers,
                json={"requestId": "req-1", "decision": "once", "policyRevision": 1})
        self.assertEqual(response.status_code, 409, response.text)

    async def test_permission_denied_without_auth(self):
        response = await self.http.post("/api/v1/web/tasks/t1/permission",
            json={"requestId": "req-1", "decision": "once", "policyRevision": 1})
        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
