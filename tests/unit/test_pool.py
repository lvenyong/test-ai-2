"""Unit tests for database connection pool management.

asyncpg's create_pool is mocked; tests verify parameter passing, multi-database
creation, and graceful/forced shutdown paths.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.db.pool import close_pools, create_pool, create_pools


class TestCreatePool:
    """Tests for single-pool creation."""

    async def test_create_pool_passes_config(self) -> None:
        mock_pool = MagicMock()
        config = DatabaseConfig(
            name="hospital", host="db.local", port=6543, user="svc",
            password="pw", min_pool_size=2, max_pool_size=9,
            pool_timeout=15.0, command_timeout=25.0,
        )
        with patch(
            "pg_mcp.db.pool.asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)
        ) as factory:
            pool = await create_pool(config)
        assert pool is mock_pool
        kwargs = factory.call_args.kwargs
        assert kwargs["host"] == "db.local"
        assert kwargs["port"] == 6543
        assert kwargs["database"] == "hospital"
        assert kwargs["user"] == "svc"
        assert kwargs["password"] == "pw"
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 9
        assert kwargs["timeout"] == 15.0
        assert kwargs["command_timeout"] == 25.0

    async def test_create_pool_none_raises(self) -> None:
        config = DatabaseConfig(name="broken")
        with (
            patch("pg_mcp.db.pool.asyncpg.create_pool", new=AsyncMock(return_value=None)),
            pytest.raises(RuntimeError, match="broken"),
        ):
            await create_pool(config)

    async def test_create_pool_propagates_connection_error(self) -> None:
        config = DatabaseConfig(name="unreachable")
        error = OSError("connection refused")
        with (
            patch("pg_mcp.db.pool.asyncpg.create_pool", new=AsyncMock(side_effect=error)),
            pytest.raises(OSError, match="connection refused"),
        ):
            await create_pool(config)


class TestCreatePools:
    """Tests for multi-database pool creation."""

    async def test_creates_pool_per_config(self) -> None:
        pools_by_name = {name: MagicMock(spec=["close"]) for name in ("db1", "db2")}
        configs = [DatabaseConfig(name="db1"), DatabaseConfig(name="db2")]

        async def fake_create(config: DatabaseConfig):
            return pools_by_name[config.name]

        with patch("pg_mcp.db.pool.create_pool", new=fake_create):
            pools = await create_pools(configs)
        assert set(pools) == {"db1", "db2"}


class TestClosePools:
    """Tests for graceful and forced shutdown."""

    async def test_graceful_close(self) -> None:
        pool = MagicMock()
        pool.close = AsyncMock()
        await close_pools({"db": pool})
        pool.close.assert_awaited_once()
        pool.terminate.assert_not_called()

    async def test_timeout_forces_terminate(self) -> None:
        async def slow_close() -> None:
            await asyncio.sleep(5)

        pool = MagicMock()
        pool.close = slow_close
        pool.terminate = MagicMock()
        await close_pools({"db": pool}, timeout=0.05)
        pool.terminate.assert_called_once()

    async def test_error_forces_terminate(self) -> None:
        pool = MagicMock()
        pool.close = AsyncMock(side_effect=ValueError("already closing"))
        pool.terminate = MagicMock()
        await close_pools({"db": pool})
        pool.terminate.assert_called_once()

    async def test_close_multiple_pools(self) -> None:
        pools = {}
        for name in ("a", "b"):
            pool = MagicMock()
            pool.close = AsyncMock()
            pools[name] = pool
        await close_pools(pools)
        for pool in pools.values():
            pool.close.assert_awaited_once()
