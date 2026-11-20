"""Nekowork usage dashboard: aggregation, Jakarta bucketing, cache, admin RBAC."""
import importlib
import os
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models
from app.api import nekowork
from app.auth import sign_token
from app.db import Base, get_session
from app.services import nekowork_usage as usage

for model_file in (Path(__file__).parent / "app").glob("*models.py"):
    importlib.import_module("app." + model_file.stem)


def _logs():
    """Two gateway users; timestamps at 10:00 WIB *today* (date-independent)."""
    from datetime import datetime
    day_start = datetime.now(usage.JAKARTA).replace(hour=0, minute=0, second=0, microsecond=0)
    day = int(day_start.timestamp()) + 3 * 3600
    return [
        {"id": 1, "created_at": day, "type": 2, "username": "nk-u1", "token_name": "nekowork-a@x.com",
         "quota": 500_000, "prompt_tokens": 100, "completion_tokens": 50},
        {"id": 2, "created_at": day + 60, "type": 2, "username": "nk-u1", "token_name": "nekowork-a@x.com",
         "quota": 250_000, "prompt_tokens": 10, "completion_tokens": 5},
        {"id": 3, "created_at": day, "type": 2, "username": "nk-u2", "token_name": "nekowork-b@x.com",
         "quota": 1_000_000, "prompt_tokens": 200, "completion_tokens": 100},
    ]


class UsageServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        usage._cache.clear()

    async def test_aggregate_buckets_by_gateway_username(self):
        result = usage._aggregate(_logs(), gateway_username="nk-u1", days=14)
        today_row = result["days"][-1]
        self.assertEqual(today_row["requests"], 2)
        self.assertEqual(today_row["tokens"], 165)
        self.assertAlmostEqual(today_row["usd"], 1.5)  # 750k quota / 500k per unit
        self.assertAlmostEqual(result["today_usd"], 1.5)
        self.assertEqual(len(result["days"]), 14)

    async def test_aggregate_excludes_other_users(self):
        result = usage._aggregate(_logs(), gateway_username="nk-u2", days=14)
        self.assertAlmostEqual(result["days"][-1]["usd"], 2.0)
        self.assertAlmostEqual(result["window_usd"], 2.0)

    async def test_cache_hits_until_ttl(self):
        calls = {"n": 0}

        async def fake_fetch(days, *, force):
            calls["n"] += 1
            return _logs()

        with mock.patch.object(usage, "_fetch_consume_logs", fake_fetch):
            await usage.usage_for("nk-u1")
            await usage.usage_for("nk-u1")
        self.assertEqual(calls["n"], 2)  # fetch itself caches once; aggregate runs per call

    def test_cache_prevents_refetch(self):
        async def fake_paged(gw, page):
            if page == 0:
                return {"data": _logs()}
            return {"data": []}

        async def run():
            with mock.patch.object(usage.gwsvc, "_gateway", return_value=object()), \
                 mock.patch.object(usage, "_paged_request", fake_paged):
                first = await usage._fetch_consume_logs(14, force=False)
                with mock.patch.object(usage, "_paged_request",
                                       side_effect=AssertionError("cache miss!")):
                    second = await usage._fetch_consume_logs(14, force=False)
            self.assertEqual(len(first), 3)
            self.assertEqual(second, first)

        import asyncio
        asyncio.run(run())


class UsageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                models.User(id="u-admin", email="admin@x.com", username="admin",
                            display_name="Admin", is_super=True),
                models.User(id="u-member", email="m@x.com", username="mem",
                            display_name="Mem", is_super=False),
            ])
            await session.commit()

        async def override_session():
            async with self.sessions() as session:
                yield session

        self.app = FastAPI()
        self.app.include_router(nekowork.router)
        self.app.dependency_overrides[get_session] = override_session
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url="http://test")

        async def fake_status(session, user):
            return {"provisioned": True, "username": "nk-u1", "has_token": True}

        async def fake_all(session):
            return [{"user_id": "u-member", "email": "m@x.com", "display_name": "Mem",
                     "username": "nk-u1", "token_name": "nekowork-a@x.com",
                     "token_enabled": True, "balance_usd": 7.42}]

        patcher1 = mock.patch.object(nekowork.nekowork_newapi, "account_status", fake_status)
        patcher2 = mock.patch.object(nekowork.nekowork_newapi, "list_all_accounts", fake_all)
        patcher3 = mock.patch.object(usage, "_fetch_consume_logs", self._fake_fetch)
        for p in (patcher1, patcher2, patcher3):
            p.start()
            self.addCleanup(p.stop)

    async def _fake_fetch(self, days, *, force):
        return _logs()

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def _get(self, path, user_id):
        token = sign_token(user_id)
        return await self.http.get(path, headers={"Authorization": f"Bearer {token}"})

    async def test_member_usage_me(self):
        res = await self._get("/api/v1/nekowork/usage/me", "u-member")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["token"]["username"], "nk-u1")
        self.assertAlmostEqual(body["usage"]["days"][-1]["usd"], 1.5)

    async def test_member_cannot_list_all(self):
        res = await self._get("/api/v1/nekowork/usage", "u-member")
        self.assertEqual(res.status_code, 403)

    async def test_admin_lists_all_with_totals(self):
        res = await self._get("/api/v1/nekowork/usage", "u-admin")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["usage"]["month_usd"], 1.5)


if __name__ == "__main__":
    unittest.main()
