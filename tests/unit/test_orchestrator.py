"""Unit tests for QueryOrchestrator.

This module tests the orchestrator's coordination of the query pipeline,
including multi-database dispatch, retry logic with backoff, rate limiting,
metrics recording, and integration with all components.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    LLMError,
    RateLimitExceededError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator


def make_orchestrator(
    generator: object,
    validator: object,
    executor: object,
    result_validator: object,
    schema_cache: object,
    pools: dict,
    resilience_config: ResilienceConfig | None = None,
    validation_config: ValidationConfig | None = None,
    database_name: str = "test_db",
    rate_limiter: MultiRateLimiter | None = None,
) -> QueryOrchestrator:
    """Create an orchestrator with mocked components for a single database."""
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validators={database_name: validator},
        sql_executors={database_name: executor},
        result_validator=result_validator,
        schema_cache=schema_cache,
        pools=pools,
        resilience_config=resilience_config or ResilienceConfig(),
        validation_config=validation_config or ValidationConfig(),
        rate_limiter=rate_limiter,
    )


class TestDatabaseResolution:
    """Test database name resolution logic."""

    @pytest.fixture
    def mock_pools(self) -> dict[str, MagicMock]:
        """Create mock connection pools."""
        return {
            "db1": MagicMock(),
            "db2": MagicMock(),
        }

    @pytest.fixture
    def orchestrator(self, mock_pools: dict[str, MagicMock]) -> QueryOrchestrator:
        """Create orchestrator with mocked components."""
        return QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validators={"db1": MagicMock(), "db2": MagicMock()},
            sql_executors={"db1": MagicMock(), "db2": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools=mock_pools,
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

    def test_resolve_database_specified_valid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified valid database."""
        result = orchestrator._resolve_database("db1")
        assert result == "db1"

    def test_resolve_database_specified_invalid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified but invalid database."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database("nonexistent")

        assert "not found" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]
        assert "db2" in exc_info.value.details["available_databases"]

    def test_resolve_database_auto_select_single(self) -> None:
        """Test auto-selecting when only one database available."""
        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validators={"only_db": MagicMock()},
            sql_executors={"only_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"only_db": MagicMock()},
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        result = orchestrator._resolve_database(None)
        assert result == "only_db"

    def test_resolve_database_auto_select_multiple_fails(
        self, orchestrator: QueryOrchestrator
    ) -> None:
        """Test that auto-select fails when multiple databases available."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "multiple databases" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]

    def test_resolve_database_no_databases(self) -> None:
        """Test error when no databases configured."""
        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validators={},
            sql_executors={},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={},
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "no databases configured" in str(exc_info.value).lower()


class TestSQLGenerationWithRetry:
    """Test SQL generation with retry logic."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(
            database_name="test_db",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="integer",
                            is_nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnInfo(
                            name="name",
                            data_type="varchar(255)",
                            is_nullable=False,
                        ),
                    ],
                )
            ],
            version="15.0",
        )

    @pytest.mark.asyncio
    async def test_generate_sql_success_first_attempt(self, mock_schema: DatabaseSchema) -> None:
        """Test successful SQL generation on first attempt."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT * FROM users;", 42)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None  # No exception = valid

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=3),
        )

        # Execute
        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            database_name="test_db",
            request_id="test-123",
        )

        # Verify
        assert sql == "SELECT * FROM users;"
        assert tokens == 42
        assert validation_result.is_valid is True
        assert validation_result.is_select is True
        mock_generator.generate.assert_called_once()
        mock_validator.validate_or_raise.assert_called_once_with("SELECT * FROM users;")

    @pytest.mark.asyncio
    async def test_generate_sql_retry_on_validation_failure(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test retry logic when validation fails."""
        # Setup mocks - first attempt fails validation, second succeeds
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = [
            ("SELECT * FROM user;", 10),  # First attempt (wrong table name)
            ("SELECT * FROM users;", 15),  # Second attempt (correct)
        ]

        mock_validator = MagicMock()
        # First call raises error, second call succeeds
        mock_validator.validate_or_raise.side_effect = [
            SQLParseError('relation "user" does not exist'),
            None,  # Success on second attempt
        ]

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=3),
        )

        # Execute
        sql, validation_result, tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            database_name="test_db",
            request_id="test-123",
        )

        # Verify
        assert sql == "SELECT * FROM users;"
        assert tokens == 25  # Tokens from both attempts accumulate
        assert validation_result.is_valid is True
        assert mock_generator.generate.call_count == 2
        assert mock_validator.validate_or_raise.call_count == 2

        # Verify retry included error feedback
        second_call = mock_generator.generate.call_args_list[1]
        assert second_call.kwargs["previous_attempt"] == "SELECT * FROM user;"
        assert 'relation "user" does not exist' in second_call.kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_generate_sql_backoff_between_retries(
        self, mock_schema: DatabaseSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test exponential backoff sleep between validation-failure retries."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("DELETE FROM users;", 5)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError("not allowed")

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=2, retry_delay=0.5, backoff_factor=2.0),
        )

        with pytest.raises(SecurityViolationError):
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=mock_schema,
                database_name="test_db",
                request_id="test-123",
            )

        # Delays: retry_delay * backoff_factor ** attempt for attempts 0 and 1
        assert sleep_calls == [pytest.approx(0.5), pytest.approx(1.0)]

    @pytest.mark.asyncio
    async def test_generate_sql_fails_after_max_retries(self, mock_schema: DatabaseSchema) -> None:
        """Test failure after exhausting all retries."""
        # Setup mocks - all attempts fail validation
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("DELETE FROM users;", 5)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE statements are not allowed"
        )

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=2),
        )

        # Execute and verify exception
        with pytest.raises(SecurityViolationError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=mock_schema,
                database_name="test_db",
                request_id="test-123",
            )

        assert "DELETE statements are not allowed" in str(exc_info.value)
        # Should attempt max_retries + 1 times (initial + retries)
        assert mock_generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_circuit_breaker_open(self, mock_schema: DatabaseSchema) -> None:
        """Test that open circuit breaker prevents SQL generation."""
        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(circuit_breaker_threshold=1),
        )

        # Manually open the circuit breaker
        orchestrator.circuit_breaker._state = CircuitState.OPEN
        orchestrator.circuit_breaker._failure_count = 5

        # Attempt should fail immediately
        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                database_name="test_db",
                request_id="test-123",
            )

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert "circuit breaker" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_sql_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors during generation."""
        mock_generator = AsyncMock()
        mock_generator.generate.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=1),
        )

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                database_name="test_db",
                request_id="test-123",
            )

        assert "unexpectedly" in str(exc_info.value).lower()
        assert orchestrator.circuit_breaker.failure_count == 1


class TestRateLimiting:
    """Test rate limiter integration."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(database_name="test_db", tables=[], version="15.0")

    @pytest.mark.asyncio
    async def test_query_rate_limit_exceeded(self, mock_schema: DatabaseSchema) -> None:
        """Test that a query rejected by the rate limiter returns a rate limit error."""
        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            validator=MagicMock(),
            executor=AsyncMock(),
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(rate_limit_timeout=0.1),
            validation_config=ValidationConfig(enabled=False),
            rate_limiter=rate_limiter,
        )

        # Hold the only query slot
        assert await rate_limiter.query_limiter.acquire() is True

        request = QueryRequest(question="Get users", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"
        rate_limiter.query_limiter.release()

    @pytest.mark.asyncio
    async def test_llm_rate_limit_exceeded(self, mock_schema: DatabaseSchema) -> None:
        """Test that an LLM call rejected by the rate limiter raises."""
        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(rate_limit_timeout=0.1),
            rate_limiter=rate_limiter,
        )

        # Hold the only LLM slot
        assert await rate_limiter.llm_limiter.acquire() is True

        with pytest.raises(RateLimitExceededError):
            await orchestrator._generate_sql_with_retry(
                question="Get users",
                schema=mock_schema,
                database_name="test_db",
                request_id="test-123",
            )

        rate_limiter.llm_limiter.release()

    @pytest.mark.asyncio
    async def test_query_slot_released_after_success(self, mock_schema: DatabaseSchema) -> None:
        """Test the query slot is released after a successful query."""
        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", 1)

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=False),
            rate_limiter=rate_limiter,
        )

        request = QueryRequest(question="Test", database="test_db", return_type=ReturnType.SQL)
        response = await orchestrator.execute_query(request)

        assert response.success is True
        # The released slot must be immediately re-acquirable
        assert await rate_limiter.query_limiter.acquire(timeout=0.1) is True
        rate_limiter.query_limiter.release()

    @pytest.mark.asyncio
    async def test_query_slot_released_after_error(self, mock_schema: DatabaseSchema) -> None:
        """Test the query slot is released when the pipeline raises."""
        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("boom")

        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            rate_limiter=rate_limiter,
        )

        request = QueryRequest(question="Test", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        # The released slot must be immediately re-acquirable
        assert await rate_limiter.query_limiter.acquire(timeout=0.1) is True
        rate_limiter.query_limiter.release()


class TestMetricsRecording:
    """Test Prometheus metrics integration (with a metrics spy)."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(database_name="test_db", tables=[], version="15.0")

    @pytest.fixture
    def metrics_spy(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        """Replace the orchestrator's metrics singleton with a spy."""
        spy = MagicMock()
        monkeypatch.setattr("pg_mcp.services.orchestrator.metrics", spy)
        return spy

    @pytest.mark.asyncio
    async def test_success_metrics_recorded(
        self, mock_schema: DatabaseSchema, metrics_spy: MagicMock
    ) -> None:
        """Test metrics recorded for a successful query with results."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_executor = AsyncMock()
        mock_executor.execute.return_value = ([{"id": 1}], 1)

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="ok",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            validator=MagicMock(),
            executor=mock_executor,
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=True),
        )
        orchestrator.sql_generator.generate.return_value = ("SELECT 1;", 7)

        request = QueryRequest(question="Test", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is True
        metrics_spy.query_duration.observe.assert_called_once()
        metrics_spy.increment_query_request.assert_called_once_with("success", "test_db")
        metrics_spy.increment_llm_call.assert_any_call("generate_sql")
        metrics_spy.increment_llm_call.assert_any_call("validate_result")
        metrics_spy.increment_llm_tokens.assert_called_once_with("generate_sql", 7)
        metrics_spy.observe_db_query_duration.assert_called_once()
        metrics_spy.observe_llm_latency.assert_called()

    @pytest.mark.asyncio
    async def test_error_metrics_recorded(
        self, mock_schema: DatabaseSchema, metrics_spy: MagicMock
    ) -> None:
        """Test metrics recorded for a failed query."""
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("boom")

        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
        )

        request = QueryRequest(question="Test", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        metrics_spy.increment_query_request.assert_called_once_with("error", "test_db")
        metrics_spy.query_duration.observe.assert_called_once()

    @pytest.mark.asyncio
    async def test_sql_rejected_metric_on_security_violation(self, metrics_spy: MagicMock) -> None:
        """Test the sql_rejected counter records security violations."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("DELETE FROM users;", 5)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError("nope")

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=0),
        )

        with pytest.raises(SecurityViolationError):
            await orchestrator._generate_sql_with_retry(
                question="Delete users",
                schema=DatabaseSchema(database_name="test_db", tables=[]),
                database_name="test_db",
                request_id="test-123",
            )

        metrics_spy.increment_sql_rejected.assert_called_once_with("security_violation")


class TestResultValidation:
    """Test result validation logic."""

    @pytest.mark.asyncio
    async def test_validate_results_success(self) -> None:
        """Test successful result validation."""
        mock_validator = AsyncMock()
        mock_validator.validate.return_value = ResultValidationResult(
            confidence=85,
            explanation="Results match the question well",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            generator=MagicMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=mock_validator,
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=True),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 85
        mock_validator.validate.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_results_disabled(self) -> None:
        """Test that validation is skipped when disabled."""
        mock_validator = AsyncMock()

        orchestrator = make_orchestrator(
            generator=MagicMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=mock_validator,
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=False),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100
        mock_validator.validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_results_failure_does_not_raise(self) -> None:
        """Test that validation failures don't raise exceptions."""
        mock_validator = AsyncMock()
        mock_validator.validate.side_effect = Exception("Validation failed")

        orchestrator = make_orchestrator(
            generator=MagicMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=mock_validator,
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=True),
        )

        # Should not raise, returns default confidence
        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100


class TestExecuteQueryFlow:
    """Test complete query execution flow."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(
            database_name="test_db",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="integer",
                            is_nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnInfo(
                            name="name",
                            data_type="varchar(255)",
                            is_nullable=False,
                        ),
                    ],
                )
            ],
            version="15.0",
        )

    @pytest.mark.asyncio
    async def test_execute_query_sql_only(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=SQL."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT * FROM users;", 12)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        assert response.generated_sql == "SELECT * FROM users;"
        assert response.validation is not None
        assert response.validation.is_valid is True
        assert response.data is None  # No execution for SQL-only
        assert response.error is None
        assert response.tokens_used == 12

    @pytest.mark.asyncio
    async def test_execute_query_with_results(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=RESULT."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT id, name FROM users;", 20)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.execute.return_value = (
            [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
            ],
            2,  # total count
        )

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="Good results",
            suggestion=None,
            is_acceptable=True,
        )

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=mock_executor,
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=True),
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        assert response.generated_sql == "SELECT id, name FROM users;"
        assert response.data is not None
        assert response.data.row_count == 2
        assert response.data.total_count == 2
        assert len(response.data.rows) == 2
        assert response.data.columns == ["id", "name"]
        assert response.confidence == 90
        assert response.tokens_used == 20
        assert response.error is None

    @pytest.mark.asyncio
    async def test_execute_query_total_count_preserved(self, mock_schema: DatabaseSchema) -> None:
        """Test that the pre-truncation total count reaches the response."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT id FROM users;", 5)

        mock_executor = AsyncMock()
        mock_executor.execute.return_value = ([{"id": 1}, {"id": 2}], 5)

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=MagicMock(),
            executor=mock_executor,
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=False),
        )

        request = QueryRequest(question="Get users", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.data is not None
        assert response.data.row_count == 2  # Rows actually returned
        assert response.data.total_count == 5  # Total before truncation

    @pytest.mark.asyncio
    async def test_execute_query_multi_database_dispatch(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test the request executes against the executor of the requested database."""
        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", 3)

        validator_for = {"db1": MagicMock(), "db2": MagicMock()}
        executor_for = {"db1": AsyncMock(), "db2": AsyncMock()}
        executor_for["db2"].execute.return_value = ([{"x": 1}], 1)

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validators=validator_for,
            sql_executors=executor_for,
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"db1": MagicMock(), "db2": MagicMock()},
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=False),
        )

        request = QueryRequest(question="Test", database="db2")
        response = await orchestrator.execute_query(request)

        assert response.success is True
        executor_for["db2"].execute.assert_called_once_with("SELECT 1;")
        executor_for["db1"].execute.assert_not_called()
        validator_for["db2"].validate_or_raise.assert_called_once_with("SELECT 1;")
        validator_for["db1"].validate_or_raise.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_query_request_id_in_success_response(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test the response carries a UUID request_id on success."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", 1)

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            validation_config=ValidationConfig(enabled=False),
        )

        request = QueryRequest(question="Test", database="test_db", return_type=ReturnType.SQL)
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.request_id is not None
        uuid.UUID(response.request_id)  # Raises if not a valid UUID

    @pytest.mark.asyncio
    async def test_execute_query_request_id_in_error_response(self) -> None:
        """Test the response carries a UUID request_id on error."""
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("boom")

        orchestrator = make_orchestrator(
            generator=MagicMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
        )

        request = QueryRequest(question="Test", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.request_id is not None
        uuid.UUID(response.request_id)  # Raises if not a valid UUID

    @pytest.mark.asyncio
    async def test_execute_query_schema_not_cached(self) -> None:
        """Test loading schema when not in cache."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = None  # Not in cache
        mock_cache.load = AsyncMock(return_value=mock_schema)

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", 1)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_pool = MagicMock()

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": mock_pool},
        )

        # Execute
        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify schema was loaded
        mock_cache.load.assert_called_once_with("test_db", mock_pool)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_load_fails(self) -> None:
        """Test handling of schema load failure."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.load = AsyncMock(side_effect=Exception("DB connection failed"))

        orchestrator = make_orchestrator(
            generator=MagicMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
        )

        # Execute
        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "schema" in response.error.message.lower()
        assert response.generated_sql is None

    @pytest.mark.asyncio
    async def test_execute_query_validation_error(self) -> None:
        """Test handling of SQL validation errors."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("DELETE FROM users;", 5)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError("DELETE not allowed")

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            resilience_config=ResilienceConfig(max_retries=1),
        )

        # Execute
        request = QueryRequest(
            question="Delete all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "DELETE not allowed" in response.error.message
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_execute_query_execution_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of SQL execution errors."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT * FROM users;", 5)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.execute.side_effect = DatabaseError("Query execution failed")

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=mock_executor,
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "execution failed" in response.error.message.lower()
        assert response.error.code == "database_error"

    @pytest.mark.asyncio
    async def test_execute_query_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(
            generator=MagicMock(),
            validator=MagicMock(),
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert "internal server error" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_query_auto_select_database(self, mock_schema: DatabaseSchema) -> None:
        """Test auto-selecting database when only one available."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate.return_value = ("SELECT 1;", 1)

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            generator=mock_generator,
            validator=mock_validator,
            executor=MagicMock(),
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"only_db": MagicMock()},  # Only one database
            database_name="only_db",
        )

        # Execute without specifying database
        request = QueryRequest(
            question="Test query",
            database=None,  # No database specified
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        # Verify schema was fetched for auto-selected database
        mock_cache.get.assert_called_once_with("only_db")
