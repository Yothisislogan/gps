"""Exercise the real Database state machine with a controllable async pool."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest

from api.clients.db import Database
from api.errors import ApiError


class Cursor:
    description = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, sql, params):
        return None

    async def fetchall(self):
        return [{"ready": 1}]

    async def fetchone(self):
        return {"id": "saved"}


class Connection:
    row_factory: Any = None

    def cursor(self, **kwargs):
        return Cursor()


@pytest.fixture
def pools(monkeypatch):
    class Pool:
        online = False
        instances: ClassVar[list] = []

        def __init__(self, *args, **kwargs):
            self.closed = False
            self.instances.append(self)

        async def open(self, **kwargs):
            if not self.online:
                raise RuntimeError("database starting")

        async def close(self):
            self.closed = True

        @asynccontextmanager
        async def connection(self, **kwargs):
            yield Connection()

    monkeypatch.setattr("psycopg_pool.AsyncConnectionPool", Pool)
    return Pool


def test_failed_startup_recovers_without_restarting_the_api(pools):
    async def scenario():
        database = Database("postgresql://test")
        await database.connect()
        assert not database.available
        assert pools.instances[0].closed
        pools.online = True
        database._retry_at = 0
        assert await database.health()
        assert await database.insert_report("bache", 12.1, -86.2, None, None, None) == "saved"
        await database.close()

    asyncio.run(scenario())


def test_concurrent_requests_share_one_recovery_attempt(pools):
    async def scenario():
        database = Database("postgresql://test")
        pools.online = True
        assert all(await asyncio.gather(*(database.health() for _ in range(10))))
        assert len(pools.instances) == 1
        await database.close()

    asyncio.run(scenario())


def test_retries_are_throttled_and_missing_database_is_not_an_empty_result(pools):
    async def scenario():
        database = Database("postgresql://test")
        for _ in range(4):
            with pytest.raises(ApiError) as error:
                await database.get_poi("missing")
            assert error.value.code == "database_unavailable"
            assert error.value.status_code == 503
        assert len(pools.instances) == 1
        await database.close()
        pools.online = True
        database._retry_at = 0
        assert not await database.health()
        assert len(pools.instances) == 1

    asyncio.run(scenario())
