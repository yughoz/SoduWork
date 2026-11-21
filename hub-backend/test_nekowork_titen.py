"""Nekowork Titen service + routes: bootstrap flow, fenced key mint, shared
projects w/ rotation, client auto-setup, proxy passthrough. External calls mocked."""
import asyncio
import hashlib
import json
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
from app.api import nekowork_titen
from app.auth import sign_token
from app.db import Base, get_session
from app.services import nekowork_titen as svc


class FakeTiten:
    """Records /v1 API calls; canned responses for the routes the service uses."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.project_seq = 0
        self.key_seq = 0
        self.projects_by_ref: dict[str, str] = {}

    async def api(self, method, path, token=None, json_body=None):
        self.calls.append((method, path, json_body or {}))
        if path == "/v1/dashboard-sessions" and method == "POST":
            self.key_seq += 1
            return {"api_key": f"sess-{self.key_seq}", "organization_id": "org_test"}
        if path == "/v1/operator-accounts/current/password":
            return {}
        if path == "/v1/grants" and method == "POST":
            return {"grant_id": "grant_probe"}
        if path == "/v1/keys" and method == "POST":
            assert "kind" not in json_body, "use principal_kind"
            self.key_seq += 1
            return {"api_key": f"titen_sk_raw{self.key_seq}", "key_id": f"key_{self.key_seq:03d}",
                    "principal_id": f"agent_{self.key_seq:012d}"}
        if path == "/v1/projects/resolve":
            ref = json_body["reference"]
            if ref not in self.projects_by_ref:
                self.project_seq += 1
                self.projects_by_ref[ref] = f"proj_{self.project_seq:03d}"
            return {"project_id": self.projects_by_ref[ref], "reference": ref}
        if method == "PATCH":
            return {"project_id": path.split("/")[-1]}
        if method == "DELETE":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def calls_to(self, path_prefix):
        return [c for c in self.calls if c[1].startswith(path_prefix)]


class TitenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                models.User(id="u1", email="budi@x.com", username="budi", display_name="Budi"),
                models.User(id="adm", email="admin@x.com", username="admin", is_super=True),
                models.Client(id="dev1", user_ref="u1", mcp_servers=[{"name": "other"}]),
            ])
            await session.commit()

        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.fake = FakeTiten()
        patchers = [
            mock.patch.object(svc, "_db_path", lambda: self.tmp / "titen.db"),
            mock.patch.object(svc, "_meta_path", lambda: self.tmp / "titen-meta.json"),
            mock.patch.object(svc, "_api", self.fake.api),
            mock.patch.object(svc, "_healthy", mock.AsyncMock(return_value=False)),
            mock.patch.object(svc, "_run",
                              lambda args: "organization: org_abc123def456 (Nekowork)\n"
                                           "key_id: key_owner\n"
                                           "temporary_password: temp-secret\n"),
            mock.patch.object(svc.subprocess, "Popen", mock.MagicMock()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        # healthy: first probe False (triggers bootstrap/spawn), afterwards True (fast loop exit)
        self._health_probes = {"n": 0}

        async def healthy():
            self._health_probes["n"] += 1
            return self._health_probes["n"] > 1

        healthy_patcher = mock.patch.object(svc, "_healthy", healthy)
        healthy_patcher.start()
        self.addCleanup(healthy_patcher.stop)
        svc._lock = asyncio.Lock()

        async def override_session():
            async with self.sessions() as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(nekowork_titen.router)
        self.app.dependency_overrides[get_session] = override_session
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    def _admin_headers(self):
        return {"Authorization": "Bearer " + sign_token("adm")}

    def _meta(self):
        return json.loads((self.tmp / "titen-meta.json").read_text())

    async def test_ensure_server_bootstraps_and_spawns(self):
        result = await svc.ensure_server()
        self.assertTrue(result["running"])
        meta = self._meta()
        self.assertEqual(meta["org_id"], "org_abc123def456")
        self.assertEqual((self.tmp / "titen-meta.json").stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(meta["owner_password"], "temp-secret")  # new generated password set
        # staged login -> password change -> full login -> hub key -> org grant
        self.assertEqual(len(self.fake.calls_to("/v1/dashboard-sessions")), 2)
        grants = self.fake.calls_to("/v1/grants")
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0][2]["permissions"], ["read", "write", "approve", "admin"])
        self.assertEqual(grants[0][2]["target_type"], "organization")
        self.assertTrue(any(c[1] == "/v1/keys" and c[2].get("scopes") == ["*"]
                            for c in self.fake.calls_to("/v1/keys")))

    async def test_ensure_server_idempotent_when_healthy(self):
        self._health_probes["n"] = 1  # next probe returns True immediately
        result = await svc.ensure_server()
        self.assertFalse(result["spawned"])
        self.assertEqual(self.fake.calls, [])

    async def _mint(self, user_id="u1", username="budi", extra=None):
        async with self.sessions() as session:
            return await svc.mint_user_key(session, user_id, username=username,
                                           extra_projects=extra)

    async def test_mint_user_key_fences_writes_and_sets_up_clients(self):
        await svc.ensure_server()
        result = await self._mint()
        self.assertTrue(result["token"].startswith("titen_sk_"))
        mint = [c for c in self.fake.calls_to("/v1/keys") if c[0] == "POST"][-1]
        self.assertEqual(mint[2]["label"], "user:u1")
        self.assertEqual(mint[2]["write_projects"], [result["project_id"]])
        self.assertEqual(mint[2]["max_trust"], "asserted")
        self.assertNotIn("read_projects", mint[2])  # read left to visibility
        async with self.sessions() as session:
            row = (await session.execute(select(svc.TitenKey))).scalar_one()
            self.assertEqual(row.key_hash, hashlib.sha256(result["token"].encode()).hexdigest())
            device = (await session.execute(select(models.Client))).scalar_one()
            entries = [s for s in device.mcp_servers if s["name"] == "nekowork-titen"]
            self.assertEqual(len(entries), 1)
            self.assertIn(result["token"], entries[0]["headers"]["Authorization"])
            # other pre-existing entries untouched
            self.assertIn("other", [s["name"] for s in device.mcp_servers])

    async def test_remint_revokes_old_key(self):
        await svc.ensure_server()
        first = await self._mint()
        second = await self._mint()
        self.assertNotEqual(first["key_id"], second["key_id"])
        deletes = [c for c in self.fake.calls if c[0] == "DELETE"]
        self.assertEqual([c[1] for c in deletes], [f"/v1/keys/{first['key_id']}"])
        async with self.sessions() as session:
            active = (await session.execute(select(svc.TitenKey).where(
                svc.TitenKey.status == "active"))).scalars().all()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].key_id, second["key_id"])

    async def test_shared_project_organization_visibility(self):
        await svc.ensure_server()
        async with self.sessions() as session:
            row = await svc.create_shared_project(session, "Kopdes Merdeka", "adm")
        self.assertTrue(row.reference.startswith("shared/"))
        patch = [c for c in self.fake.calls if c[0] == "PATCH"][-1]
        self.assertEqual(patch[2], {"default_visibility": "organization"})

    async def test_member_add_rotates_key_with_combined_fence_remove_back_to_own(self):
        await svc.ensure_server()
        async with self.sessions() as session:
            shared = await svc.create_shared_project(session, "Kopdes", "adm")
        own_before = (await self._mint())["project_id"]
        async with self.sessions() as session:
            added = await svc.set_shared_member(session, shared, "u1", joined=True)
        self.assertIn(shared.project_gid, added["write_projects"])
        self.assertIn(own_before, added["write_projects"])
        async with self.sessions() as session:
            removed = await svc.set_shared_member(session, shared, "u1", joined=False)
        self.assertEqual(removed["write_projects"], [own_before])
        async with self.sessions() as session:
            members = (await session.execute(select(svc.TitenSharedMember))).scalars().all()
            self.assertEqual(len(members), 0)

    async def test_admin_gating_and_show_once_route(self):
        member = {"Authorization": "Bearer " + sign_token("u1")}
        res = await self.http.get("/api/v1/nekowork/titen/keys", headers=member)
        self.assertEqual(res.status_code, 403)
        res = await self.http.post("/api/v1/nekowork/titen/keys", json={"user": "budi@x.com"},
                                   headers=self._admin_headers())
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["token"].startswith("titen_sk_"))  # shown exactly once here
        res = await self.http.get("/api/v1/nekowork/titen/keys", headers=self._admin_headers())
        items = res.json()["items"]
        self.assertNotIn("token", items[0])  # list never leaks raw
        res = await self.http.post("/api/v1/nekowork/titen/keys", json={"user": "ghost@x.com"},
                                   headers=self._admin_headers())
        self.assertEqual(res.status_code, 404)

    async def test_me_and_rotate(self):
        res = await self.http.get("/api/v1/nekowork/titen/me",
                                  headers={"Authorization": "Bearer " + sign_token("u1")})
        self.assertFalse(res.json()["has_key"])
        await svc.ensure_server()
        res = await self.http.post("/api/v1/nekowork/titen/me/rotate", json={},
                                   headers={"Authorization": "Bearer " + sign_token("u1")})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["token"].startswith("titen_sk_"))
        res = await self.http.get("/api/v1/nekowork/titen/me",
                                  headers={"Authorization": "Bearer " + sign_token("u1")})
        self.assertTrue(res.json()["has_key"])

    async def test_shared_routes_end_to_end(self):
        res = await self.http.post("/api/v1/nekowork/titen/shared-projects",
                                   json={"name": "Kopdes Sejahtera"}, headers=self._admin_headers())
        self.assertEqual(res.status_code, 200)
        shared_id = res.json()["id"]
        res = await self.http.post(f"/api/v1/nekowork/titen/shared-projects/{shared_id}/members",
                                   json={"user": "budi@x.com"}, headers=self._admin_headers())
        self.assertEqual(res.status_code, 200)
        res = await self.http.get("/api/v1/nekowork/titen/shared-projects",
                                  headers=self._admin_headers())
        items = res.json()["items"]
        self.assertEqual(items[0]["members"], ["u1"])
        res = await self.http.delete(f"/api/v1/nekowork/titen/shared-projects/{shared_id}/members/u1",
                                     headers=self._admin_headers())
        self.assertEqual(res.status_code, 200)

    async def test_mcp_proxy_forwards_auth_and_body(self):
        captured = {}

        def handler(request):
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = request.read().decode()
            captured["path"] = request.url.path
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
                                  headers={"content-type": "application/json"})

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        class PatchedClient(real_client):
            def __init__(self, **kwargs):
                kwargs["transport"] = transport
                super().__init__(**kwargs)

        with mock.patch.object(svc.httpx, "AsyncClient", PatchedClient):
            res = await self.http.post("/api/v1/nekowork/titen/mcp",
                                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                             "params": {}},
                                       headers={"Authorization": "Bearer titen_sk_abc"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["result"]["ok"])
        self.assertEqual(captured["auth"], "Bearer titen_sk_abc")
        self.assertEqual(captured["path"], "/mcp")
        self.assertIn("initialize", captured["body"])


if __name__ == "__main__":
    unittest.main()
