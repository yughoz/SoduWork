"""Nekowork gateway: token lifecycle against a mocked New API + RBAC boundaries."""
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
from app.api import nekowork
from app.auth import sign_token
from app.db import Base, get_session
from app.services import nekowork_newapi as svc

for model_file in (Path(__file__).parent / "app").glob("*models.py"):
    importlib.import_module("app." + model_file.stem)


def _fake_gateway(monkey: mock.MagicMock, tokens_by_name: dict, users_by_name: dict, ids: dict):
    """Patch the transport layer so tests never touch the network."""

    async def fake_request(gw, method, path, *, params=None, json_body=None):
        if path == "/api/user/" and method == "GET":
            return {"data": list(users_by_name.values())}
        if path == "/api/user/" and method == "POST":
            username = json_body["username"]
            users_by_name[username] = {"id": 100 + len(users_by_name), "username": username}
            return {"success": True}
        if path == "/api/token/" and method == "GET":
            return {"data": list(tokens_by_name.values())}
        if path == "/api/token/" and method == "POST":
            name = json_body["name"]
            ids["token"] += 1
            tokens_by_name[name] = {"id": ids["token"], "name": name,
                                    "status": 1, "remain_quota": 3_710_000, "used_quota": 500_000}
            return {"success": True}
        if path.endswith("/key"):
            token_id = int(path.split("/")[3])
            return {"data": {"key": "abc123def456"}}
        if method == "DELETE" and path.startswith("/api/token/"):
            token_id = int(path.split("/")[3])
            for name, t in list(tokens_by_name.items()):
                if t["id"] == token_id:
                    del tokens_by_name[name]
            return {"success": True}
        raise AssertionError(f"unexpected call {method} {path}")

    monkey.side_effect = fake_request


class NekoworkGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(models.User(id="u-1", username="member", email="m@neko.id",
                                    display_name="Member"))
            session.add(models.User(id="u-2", username="root", email="root@neko.id",
                                    display_name="Root", is_super=True))
            await session.commit()

        self.app = FastAPI()
        self.app.include_router(nekowork.router)

        async def override_session():
            async with self.sessions() as session:
                yield session

        self.app.dependency_overrides[get_session] = override_session
        self.tokens = {"u-1": sign_token("u-1", 1), "u-2": sign_token("u-2", 1)}

        self.remote_tokens = {}
        self.remote_users = {}
        fake_gw = svc._Gateway(base_url="http://gw.test", headers={"x": "y"})
        patcher_gw = mock.patch.object(svc, "_gateway", return_value=fake_gw)
        patcher_gw.start()
        self.addCleanup(patcher_gw.stop)
        patcher = mock.patch.object(svc, "_request",
                                    new=mock.AsyncMock(side_effect=None))
        self.mocked_request = patcher.start()
        self.addCleanup(patcher.stop)
        _fake_gateway(self.mocked_request, self.remote_tokens, self.remote_users, {"token": 200})

    def _headers(self, who="u-1"):
        return {"Authorization": "Bearer " + self.tokens[who]}

    async def _post(self, path, who="u-1"):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post(path, headers=self._headers(who))

    async def _get(self, path, who="u-1"):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get(path, headers=self._headers(who))

    # ── pure helpers ──

    def test_quota_conversion(self):
        self.assertEqual(svc.quota_to_usd(500_000), 1.0)
        self.assertEqual(svc.quota_to_usd(0), 0.0)
        self.assertIsNone(svc.quota_to_usd(None))

    def test_mask_and_normalize(self):
        self.assertEqual(svc.mask_token_key("sk-abcdefghijklmnop"), "sk-a****mnop")
        self.assertEqual(svc.mask_token_key("short"), "sh****rt")
        self.assertEqual(svc.normalize_token_key("abc"), "sk-abc")
        self.assertEqual(svc.normalize_token_key("sk-abc"), "sk-abc")

    def test_deterministic_identity_matches_nekorp_format(self):
        self.assertEqual(svc.gateway_username("deadbeef"), "nk-deadbeef")
        pw1 = svc.gateway_password("u-1", "sekrit")
        pw2 = svc.gateway_password("u-1", "sekrit")
        self.assertEqual(pw1, pw2)
        self.assertNotEqual(pw1, svc.gateway_password("u-2", "sekrit"))
        self.assertLessEqual(len(pw1), 20)
        self.assertTrue(pw1.isalnum())

    def test_token_names_cover_nekorp_legacy(self):
        names = svc.legacy_token_names("m@neko.id", "u-1")
        self.assertIn("nekorp-m@neko.id", names)
        self.assertIn("nekowork-m@neko.id", names)

    # ── lifecycle ──

    async def test_generate_shows_plaintext_once_then_masked(self):
        resp = await self._post("/api/v1/nekowork/tokens/me")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["token"], "sk-abc123def456")
        self.assertEqual(body["maskedKey"], "sk-a****f456")

        status = (await self._get("/api/v1/nekowork/tokens/me")).json()
        self.assertTrue(status["has_token"])
        self.assertNotIn("plaintext", str(status))
        self.assertNotIn("sk-abc123def456", str(status))
        self.assertEqual(status["balance_usd"], 7.42)  # 3_710_000 / 500_000

    async def test_generate_twice_without_reset_conflicts(self):
        await self._post("/api/v1/nekowork/tokens/me")
        resp = await self._post("/api/v1/nekowork/tokens/me")
        self.assertEqual(resp.status_code, 409)

    async def test_reset_replaces_token(self):
        await self._post("/api/v1/nekowork/tokens/me")
        old_id = list(self.remote_tokens.values())[0]["id"]
        resp = await self._post("/api/v1/nekowork/tokens/me/reset")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(old_id, [t["id"] for t in self.remote_tokens.values()])

    async def test_revoke_clears_state(self):
        await self._post("/api/v1/nekowork/tokens/me")
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.delete("/api/v1/nekowork/tokens/me", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        status = (await self._get("/api/v1/nekowork/tokens/me")).json()
        self.assertFalse(status["has_token"])

    async def test_revoke_without_token_is_404(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.delete("/api/v1/nekowork/tokens/me", headers=self._headers())
        self.assertEqual(resp.status_code, 404)

    # ── RBAC ──

    async def test_member_cannot_list_all(self):
        resp = await self._get("/api/v1/nekowork/tokens", who="u-1")
        self.assertEqual(resp.status_code, 403)

    async def test_super_lists_accounts_without_plaintext(self):
        await self._post("/api/v1/nekowork/tokens/me")  # provision u-1
        resp = await self._get("/api/v1/nekowork/tokens", who="u-2")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("sk-abc123def456", resp.text)

    async def test_audit_rows_written(self):
        await self._post("/api/v1/nekowork/tokens/me")
        async with self.sessions() as session:
            actions = [row.action for row in (await session.execute(
                select(models.AuditLog))).scalars()]
        self.assertIn("nekowork.token.generate", actions)


if __name__ == "__main__":
    unittest.main()
