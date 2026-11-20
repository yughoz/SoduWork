"""Nekowork Asana: assignee resolver, project gating, sync idempotency, RBAC."""
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
from app.services import nekowork_asana as svc

for model_file in (Path(__file__).parent / "app").glob("*models.py"):
    importlib.import_module("app." + model_file.stem)


def _seed_cache(session):
    session.add_all([
        svc.AsanaUserCache(workspace_gid="ws1", asana_gid="111", email="budi@x.com", name="Budi"),
        svc.AsanaUserCache(workspace_gid="ws1", asana_gid="222", email="sari@x.com", name="Sari"),
        svc.AsanaUserCache(workspace_gid="ws1", asana_gid="333", email="dupe@x.com", name="Dupe A"),
        svc.AsanaUserCache(workspace_gid="ws1", asana_gid="444", email="dupe@x.com", name="Dupe B"),
        svc.AsanaProjectCache(project_gid="p1", name="Kopdes", ai_enabled=True),
        svc.AsanaProjectCache(project_gid="p2", name="Rahasia", ai_enabled=False),
        svc.AsanaProjectCache(project_gid="p3", name="Lama", archived=True, ai_enabled=True),
    ])
    return session


class AsanaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                models.User(id="u1", email="budi@x.com", username="budi", display_name="Budi"),
                models.User(id="u2", email="nomatch@x.com", username="nomatch"),
                models.User(id="u3", email="dupe@x.com", username="dupe"),
            ])
            await session.commit()
        self.admin = models.User(id="adm", email="admin@x.com", username="adm", is_super=True)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_auto_email_match_single(self):
        async with self.sessions() as session:
            _seed_cache(session)
            await session.commit()
            info = await svc.resolve_assignee(session, "u1")
            self.assertEqual(info["gid"], "111")
            self.assertEqual(info["status"], "auto")
            rows = (await session.execute(select(svc.UserIntegration))).scalars().all()
            self.assertEqual(len(rows), 1)  # auto mapping persisted

    async def test_ambiguous_email_fails_with_candidates(self):
        async with self.sessions() as session:
            _seed_cache(session)
            await session.commit()
            with self.assertRaises(svc.AsanaError) as ctx:
                await svc.resolve_assignee(session, "u3")
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("Dupe A", str(ctx.exception))

    async def test_no_match_fails_clearly(self):
        async with self.sessions() as session:
            _seed_cache(session)
            await session.commit()
            with self.assertRaises(svc.AsanaError) as ctx:
                await svc.resolve_assignee(session, "u2")
            self.assertEqual(ctx.exception.status, 422)

    async def test_manual_mapping_wins(self):
        async with self.sessions() as session:
            _seed_cache(session)
            session.add(svc.UserIntegration(user_id="u1", provider="asana", external_id="999",
                                            provider_email="other@x.com", status="manual"))
            await session.commit()
            info = await svc.resolve_assignee(session, "u1")
            self.assertEqual(info["gid"], "999")
            self.assertEqual(info["status"], "manual")

    async def test_project_gating_for_ai(self):
        async with self.sessions() as session:
            _seed_cache(session)
            await session.commit()
            self.assertEqual(await svc._validate_project(session, "p1", for_ai=True), "Kopdes")
            with self.assertRaises(svc.AsanaError):  # not ai_enabled
                await svc._validate_project(session, "p2", for_ai=True)
            with self.assertRaises(svc.AsanaError):  # archived
                await svc._validate_project(session, "p3", for_ai=True)
            self.assertEqual(await svc._validate_project(session, "p2", for_ai=False), "Rahasia")

    async def test_create_task_passes_assignee_and_project(self):
        async with self.sessions() as session:
            _seed_cache(session)
            await session.commit()

        captured = {}

        async def fake_request(method, path, *, params=None, json_body=None):
            captured["method"], captured["path"], captured["body"] = method, path, json_body
            return {"data": {"gid": "t1", "name": "Tugas", "permalink_url": "http://a/t1"}}

        async with self.sessions() as session:
            with mock.patch.object(svc, "_request", fake_request), \
                 mock.patch.object(svc, "_maton", return_value={"base": "x", "headers": {}, "workspace": "ws1"}):
                result = await svc.create_task(
                    session, self.admin,
                    {"name": "Tugas", "assignee_user": "u1", "project_gid": "p1"}, for_ai=True)
                await session.commit()
        self.assertEqual(result["gid"], "t1")
        self.assertEqual(result["assignee"], "budi@x.com")
        self.assertEqual(captured["body"]["data"]["assignee"], "111")
        self.assertEqual(captured["body"]["data"]["projects"], ["p1"])

        logs = (await session.execute(select(models.AuditLog))).scalars().all()
        self.assertTrue(any(l.action == "nekowork.asana.task_create" for l in logs))

    async def test_sync_cache_upsert_idempotent(self):
        users_page = {"data": [{"gid": "111", "email": "budi@x.com", "name": "Budi"}], "next_page": None}
        projects_page = {"data": [{"gid": "p1", "name": "Kopdes", "archived": False}], "next_page": None}

        async def fake_paged(ws, path):
            return (users_page if path == "/users" else projects_page)["data"]

        async with self.sessions() as session:
            session.add(svc.AsanaProjectCache(project_gid="p1", name="OLD", ai_enabled=True,
                                              keywords="kopdes"))
            await session.commit()
            with mock.patch.object(svc, "_paged", fake_paged), \
                 mock.patch.object(svc, "_maton", return_value={"workspace": "ws1", "headers": {}}):
                first = await svc.sync_cache(session)
                second = await svc.sync_cache(session)
            self.assertEqual((first["users"], first["projects"]), (1, 1))
            self.assertEqual((second["users"], second["projects"]), (1, 1))
            users = (await session.execute(select(svc.AsanaUserCache))).scalars().all()
            self.assertEqual(len(users), 1)
            projs = (await session.execute(select(svc.AsanaProjectCache))).scalars().all()
            self.assertEqual(len(projs), 1)          # no duplicate
            self.assertEqual(projs[0].name, "Kopdes")  # name refreshed
            self.assertTrue(projs[0].ai_enabled)       # gate preserved
            self.assertEqual(projs[0].keywords, "kopdes")


class AsanaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                models.User(id="u-admin", email="admin@x.com", username="admin", is_super=True),
                models.User(id="u-member", email="m@x.com", username="mem"),
            ])
            await session.commit()

        async def override_session():
            async with self.sessions() as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(nekowork.router)
        self.app.dependency_overrides[get_session] = override_session
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def _req(self, method, path, user_id, **kwargs):
        token = sign_token(user_id)
        return await self.http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

    async def test_member_projects_forced_ai_only(self):
        async with self.sessions() as session:
            session.add_all([
                svc.AsanaProjectCache(project_gid="p1", name="AI", ai_enabled=True),
                svc.AsanaProjectCache(project_gid="p2", name="No", ai_enabled=False),
            ])
            await session.commit()
        res = await self._req("GET", "/api/v1/nekowork/asana/projects", "u-member")
        self.assertEqual(res.status_code, 200)
        gids = [i["project_gid"] for i in res.json()["items"]]
        self.assertEqual(gids, ["p1"])

    async def test_member_cannot_sync_or_set_identity(self):
        res = await self._req("POST", "/api/v1/nekowork/asana/sync", "u-member")
        self.assertEqual(res.status_code, 403)
        res = await self._req("POST", "/api/v1/nekowork/asana/identities", "u-member",
                              json={"user_id": "u-member", "external_id": "1"})
        self.assertEqual(res.status_code, 403)

    async def test_admin_identity_set_and_delete(self):
        res = await self._req("POST", "/api/v1/nekowork/asana/identities", "u-admin",
                              json={"user_id": "u-member", "external_id": "222",
                                    "provider_email": "m@x.com", "provider_name": "Mem"})
        self.assertEqual(res.status_code, 200)
        identity_id = res.json()["id"]
        res = await self._req("DELETE", f"/api/v1/nekowork/asana/identities/{identity_id}", "u-admin")
        self.assertEqual(res.json(), {"removed": True})


if __name__ == "__main__":
    unittest.main()
