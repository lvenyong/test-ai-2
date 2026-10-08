"""Unit tests for the FastMCP server module.

Covers lifespan wiring (per-database validators/executors, rate limiter,
metrics bootstrap, extra-database fault tolerance) and the ``query`` tool
contract (question length gate, lowercase error codes, response passthrough).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp import server
from pg_mcp.config.settings import (
    DatabaseConfig,
    ObservabilityConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.query import QueryResponse
from pg_mcp.resilience.rate_limiter import MultiRateLimiter


def _make_settings(**overrides: Any) -> Settings:
    """Build a Settings object with a primary and an extra database."""
    kwargs: dict[str, Any] = {
        "openai": OpenAIConfig(api_key="sk-test"),
        "database": DatabaseConfig(name="main"),
        "security": SecurityConfig(blocked_tables=["global_secret"]),
        "resilience": ResilienceConfig(query_rate_limit=3, llm_rate_limit=2),
        "extra_databases": {
            "analytics": DatabaseConfig(
                name="analytics",
                host="db2.internal",
                blocked_tables=["raw_events"],
                allow_explain=True,
            ),
        },
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture(autouse=True)
def reset_server_globals():
    """Restore server module globals after each test."""
    yield
    server._settings = None
    server._pools = None
    server._schema_cache = None
    server._orchestrator = None


@pytest.fixture
def fake_infra(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch all external I/O used by lifespan so it can run offline.

    Returns a capture dict recording pool creation, validator kwargs,
    executor kwargs, and metrics server startup. Tests override
    ``captured["settings"]`` to control the configuration lifespan sees.
    """
    captured: dict[str, Any] = {
        "settings": _make_settings(),
        "pool_calls": [],
        "validator_kwargs": [],
        "executor_kwargs": [],
        "metrics_ports": [],
        "fail_databases": set(),
    }

    async def fake_create_pool(db_config: DatabaseConfig):
        captured["pool_calls"].append(db_config.name)
        if db_config.name in captured["fail_databases"]:
            raise ConnectionError(f"cannot connect to {db_config.name}")
        pool = MagicMock()
        pool.get_size.return_value = 5
        pool.close = AsyncMock()
        return pool

    async def fake_load(self: Any, db_name: str, _pool: Any):
        from pg_mcp.models.schema import DatabaseSchema

        return DatabaseSchema(database_name=db_name)

    def fake_validator(**kwargs: Any) -> MagicMock:
        captured["validator_kwargs"].append(kwargs)
        return MagicMock()

    def fake_executor(**kwargs: Any) -> MagicMock:
        captured["executor_kwargs"].append(kwargs)
        return MagicMock()

    metrics_mock = MagicMock()
    metrics_mock.enabled = False
    metrics_mock.start_metrics_server.side_effect = (
        lambda port: captured["metrics_ports"].append(port)
    )

    monkeypatch.setattr(server, "create_pool", fake_create_pool)
    monkeypatch.setattr(server.SchemaCache, "load", fake_load)
    monkeypatch.setattr(server, "SQLValidator", fake_validator)
    monkeypatch.setattr(server, "SQLExecutor", fake_executor)
    monkeypatch.setattr(server, "SQLGenerator", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(server, "ResultValidator", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(server, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "metrics", metrics_mock)
    monkeypatch.setattr(server, "get_settings", lambda: captured["settings"])

    captured["metrics"] = metrics_mock
    return captured


class TestLifespanWiring:
    """Tests for lifespan component construction and injection."""

    @pytest.mark.asyncio
    async def test_per_database_components_injected(
        self, fake_infra: dict[str, Any]
    ) -> None:
        """Test validators/executors are built per database and injected as dicts."""
        async with server.lifespan(None):
            orchestrator = server._orchestrator
            assert orchestrator is not None
            assert set(orchestrator.sql_validators) == {"main", "analytics"}
            assert set(orchestrator.sql_executors) == {"main", "analytics"}

            # Validators received the effective per-DB security policy:
            # the primary inherits the global list, the extra overrides it.
            policies = {
                (tuple(kw["blocked_tables"]), kw["allow_explain"])
                for kw in fake_infra["validator_kwargs"]
            }
            expected = {
                (("global_secret",), False),
                (("raw_events",), True),
            }
            assert policies == expected

            # Executors received the matching per-DB pool.
            pools_by_db = {
                kw["db_config"].name: kw["pool"] for kw in fake_infra["executor_kwargs"]
            }
            assert pools_by_db["main"] is server._pools["main"]
            assert pools_by_db["analytics"] is server._pools["analytics"]

            # Rate limiter honors resilience configuration.
            assert isinstance(orchestrator.rate_limiter, MultiRateLimiter)
            assert orchestrator.rate_limiter.query_limiter.max_concurrent == 3
            assert orchestrator.rate_limiter.llm_limiter.max_concurrent == 2

    @pytest.mark.asyncio
    async def test_skipped_extra_database_excluded(
        self, fake_infra: dict[str, Any]
    ) -> None:
        """Test a failing extra database is skipped and the server still starts."""
        fake_infra["settings"] = _make_settings(
            extra_databases={
                "broken": DatabaseConfig(name="broken", host="unreachable"),
                "analytics": DatabaseConfig(name="analytics"),
            }
        )
        fake_infra["fail_databases"].add("broken")

        async with server.lifespan(None):
            assert set(server._pools) == {"main", "analytics"}
            assert "broken" not in server._orchestrator.sql_executors
            assert "broken" not in server._orchestrator.sql_validators

    @pytest.mark.asyncio
    async def test_primary_database_failure_is_fatal(
        self, fake_infra: dict[str, Any]
    ) -> None:
        """Test a failing primary database propagates the error."""
        fake_infra["fail_databases"].add("main")

        with pytest.raises(ConnectionError):
            async with server.lifespan(None):
                pass

    @pytest.mark.asyncio
    async def test_metrics_server_not_started_when_disabled(
        self, fake_infra: dict[str, Any]
    ) -> None:
        """Test the metrics HTTP server is not started when metrics are disabled."""
        fake_infra["settings"] = _make_settings(
            observability=ObservabilityConfig(metrics_enabled=False)
        )

        async with server.lifespan(None):
            assert fake_infra["metrics"].enabled is False
            fake_infra["metrics"].start_metrics_server.assert_not_called()

    @pytest.mark.asyncio
    async def test_metrics_server_started_on_configured_port(
        self, fake_infra: dict[str, Any]
    ) -> None:
        """Test the metrics HTTP server starts on the configured port."""
        fake_infra["settings"] = _make_settings(
            observability=ObservabilityConfig(metrics_enabled=True, metrics_port=9123)
        )

        async with server.lifespan(None):
            assert fake_infra["metrics"].enabled is True
            fake_infra["metrics"].start_metrics_server.assert_called_once_with(9123)


class TestQueryTool:
    """Tests for the query tool's request handling contract."""

    @pytest.mark.asyncio
    async def test_not_initialized_returns_internal_error(self) -> None:
        """Test the tool reports internal_error when the orchestrator is missing."""
        server._orchestrator = None

        result = await server.query(question="How many users?")

        assert result["success"] is False
        assert result["error"]["code"] == "internal_error"

    @pytest.mark.asyncio
    async def test_question_too_long_rejected(self) -> None:
        """Test over-limit questions are rejected before reaching the orchestrator."""
        server._settings = _make_settings(
            validation=ValidationConfig(max_question_length=10)
        )
        server._orchestrator = MagicMock()

        result = await server.query(question="x" * 11)

        assert result["success"] is False
        assert result["error"]["code"] == "question_too_long"
        assert result["error"]["details"] == {"length": 11, "max_length": 10}
        server._orchestrator.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_return_type_rejected(self) -> None:
        """Test an invalid return_type yields the invalid_parameter code."""
        server._orchestrator = MagicMock()

        result = await server.query(question="Count users", return_type="rows")

        assert result["success"] is False
        assert result["error"]["code"] == "invalid_parameter"

    @pytest.mark.asyncio
    async def test_success_returns_response_to_dict(self) -> None:
        """Test a successful query returns the response's to_dict output."""
        response = QueryResponse(
            success=True,
            generated_sql="SELECT COUNT(*) FROM users;",
            confidence=90,
            tokens_used=42,
            request_id="req-1",
        )
        orchestrator = MagicMock()
        orchestrator.execute_query = AsyncMock(return_value=response)
        server._orchestrator = orchestrator

        result = await server.query(question="How many users?")

        assert result == response.to_dict()
        assert result["tokens_used"] == 42
        assert result["request_id"] == "req-1"
        orchestrator.execute_query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_crash_returns_internal_error(self) -> None:
        """Test an unexpected orchestrator crash yields internal_error."""
        orchestrator = MagicMock()
        orchestrator.execute_query = AsyncMock(side_effect=RuntimeError("boom"))
        server._orchestrator = orchestrator

        result = await server.query(question="How many users?")

        assert result["success"] is False
        assert result["error"]["code"] == "internal_error"
        assert "boom" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_error_codes_are_lowercase(self) -> None:
        """Test all tool error paths emit lowercase snake_case codes."""
        # Not initialized
        server._orchestrator = None
        first = await server.query(question="q")
        assert first["error"]["code"].islower()

        # Invalid parameter
        server._orchestrator = MagicMock()
        second = await server.query(question="q", return_type="bad")
        assert second["error"]["code"].islower()
