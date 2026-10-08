"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It implements retry logic with exponential backoff, rate limiting,
circuit breaker, metrics collection, and request tracking.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    ErrorDetail,
    LLMError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import metrics
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation across multiple databases. It implements retry logic with
    exponential backoff, rate limiting, circuit breaker pattern for fault
    tolerance, and Prometheus metrics collection.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validators={"mydb": validator},
        ...     sql_executors={"mydb": executor},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validators: dict[str, SQLValidator],
        sql_executors: dict[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        rate_limiter: MultiRateLimiter | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validators: SQL validation services keyed by database name.
            sql_executors: SQL execution services keyed by database name.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries, backoff,
                rate limits, and circuit breaker.
            validation_config: Validation configuration including thresholds.
            rate_limiter: Optional concurrency limiter for queries and LLM calls.
        """
        self.sql_generator = sql_generator
        self.sql_validators = sql_validators
        self.sql_executors = sql_executors
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.rate_limiter = rate_limiter

        # Create circuit breaker for LLM calls
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Start a request tracing context
        2. Acquire a query rate-limit slot
        3. Resolve and validate database name
        4. Load schema from cache
        5. Generate and validate SQL with retry and backoff
        6. Execute SQL (if return_type == RESULT)
        7. Validate results (optional)
        8. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.total_count} rows")
        """
        async with request_context() as request_id:
            logger.info(
                "Starting query execution",
                extra={"request_id": request_id, "question": request.question[:100]},
            )

            started = time.perf_counter()
            database_name: str | None = None
            status = "success"
            try:
                await self._acquire_query_slot()
                try:
                    # Step 1: Resolve database name
                    database_name = self._resolve_database(request.database)
                    return await self._process_query(request, request_id, database_name)
                finally:
                    self._release_query_slot()
            except PgMcpError as e:
                # Handle known application errors
                status = "error"
                logger.warning(
                    "Query execution failed with known error",
                    extra={
                        "request_id": request_id,
                        "error_code": e.code,
                        "error_message": str(e),
                    },
                )
                return QueryResponse(
                    success=False,
                    generated_sql=None,
                    validation=None,
                    data=None,
                    error=ErrorDetail(
                        code=e.code.value,
                        message=e.message,
                        details=e.details,
                    ),
                    confidence=0,
                    tokens_used=0,
                    request_id=request_id,
                )
            except Exception as e:
                # Handle unexpected errors
                status = "error"
                logger.exception(
                    "Query execution failed with unexpected error",
                    extra={"request_id": request_id},
                )
                return QueryResponse(
                    success=False,
                    generated_sql=None,
                    validation=None,
                    data=None,
                    error=ErrorDetail(
                        code=ErrorCode.INTERNAL_ERROR.value,
                        message=f"Internal server error: {e!s}",
                        details={"error_type": type(e).__name__},
                    ),
                    confidence=0,
                    tokens_used=0,
                    request_id=request_id,
                )
            finally:
                metrics.query_duration.observe(time.perf_counter() - started)
                metrics.increment_query_request(status, database_name or "unknown")

    async def _process_query(
        self, request: QueryRequest, request_id: str, database_name: str
    ) -> QueryResponse:
        """Run the query pipeline after tracing and rate limiting are in place.

        Args:
            request: Query request containing question and parameters.
            request_id: Request ID for tracking.
            database_name: Resolved database name.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        logger.debug(
            "Resolved database",
            extra={"request_id": request_id, "database": database_name},
        )

        # Step 2: Get schema from cache
        schema = self.schema_cache.get(database_name)
        if schema is None:
            # Schema not in cache, load it
            pool = self.pools.get(database_name)
            if pool is None:
                raise DatabaseError(
                    message=f"No connection pool available for database '{database_name}'",
                    details={"database": database_name},
                )
            try:
                schema = await self.schema_cache.load(database_name, pool)
            except Exception as e:
                raise SchemaLoadError(
                    message=f"Failed to load schema for database '{database_name}': {e!s}",
                    details={"database": database_name, "error": str(e)},
                ) from e
            metrics.set_schema_cache_age(
                database_name, self.schema_cache.get_cache_age(database_name) or 0.0
            )

        logger.debug(
            "Schema loaded",
            extra={
                "request_id": request_id,
                "database": database_name,
                "tables": len(schema.tables),
            },
        )

        # Step 3: Generate and validate SQL with retry logic
        generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
            question=request.question,
            schema=schema,
            database_name=database_name,
            request_id=request_id,
        )

        # Step 4: If return_type is SQL, return early
        if request.return_type == ReturnType.SQL:
            logger.info(
                "Returning SQL only",
                extra={"request_id": request_id, "sql_length": len(generated_sql)},
            )
            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=None,
                error=None,
                confidence=100,
                tokens_used=tokens_used,
                request_id=request_id,
            )

        # Step 5: Execute SQL
        logger.debug("Executing SQL", extra={"request_id": request_id})
        start_time = self._get_current_time_ms()
        db_started = time.perf_counter()

        results, total_count = await self.sql_executors[database_name].execute(generated_sql)

        metrics.observe_db_query_duration(time.perf_counter() - db_started)
        execution_time_ms = self._get_current_time_ms() - start_time
        logger.info(
            "SQL executed successfully",
            extra={
                "request_id": request_id,
                "row_count": total_count,
                "execution_time_ms": execution_time_ms,
            },
        )

        # Step 6: Validate results (non-blocking, failures don't fail the request)
        result_confidence = await self._validate_results_safely(
            question=request.question,
            sql=generated_sql,
            results=results,
            row_count=total_count,
            request_id=request_id,
        )

        # Step 7: Build successful response
        query_result = QueryResult(
            columns=list(results[0].keys()) if results else [],
            rows=results,
            row_count=len(results),  # Limited row count (after max_rows applied)
            total_count=total_count,  # Total matching rows before limits
            execution_time_ms=execution_time_ms,
        )

        return QueryResponse(
            success=True,
            generated_sql=generated_sql,
            validation=validation_result,
            data=query_result,
            error=None,
            confidence=result_confidence,
            tokens_used=tokens_used,
            request_id=request_id,
        )

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        database_name: str,
        request_id: str,
    ) -> tuple[str, ValidationResult, int]:
        """Generate and validate SQL with retry logic on validation failures.

        This method implements a retry loop that:
        1. Checks circuit breaker state
        2. Generates SQL using LLM (under the LLM rate-limit slot)
        3. Validates the generated SQL
        4. On validation failure, waits with exponential backoff and retries
           with error feedback
        5. Records success/failure to circuit breaker

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            database_name: Resolved database name (selects the validator).
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            RateLimitExceededError: If the LLM rate limit slot cannot be acquired.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.

        Example:
            >>> sql, validation, tokens = await orchestrator._generate_sql_with_retry(
            ...     question="Count users",
            ...     schema=db_schema,
            ...     database_name="mydb",
            ...     request_id="123",
            ... )
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        validator = self.sql_validators[database_name]
        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used = 0

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL under the LLM rate-limit slot
                async with self._llm_slot():
                    llm_started = time.perf_counter()
                    generated_sql, tokens = await self.sql_generator.generate(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_sql,
                        error_feedback=error_feedback,
                    )
                metrics.increment_llm_call("generate_sql")
                metrics.observe_llm_latency("generate_sql", time.perf_counter() - llm_started)
                metrics.increment_llm_tokens("generate_sql", tokens)
                tokens_used += tokens

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                    },
                )

                # Validate SQL
                try:
                    validator.validate_or_raise(generated_sql)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    if attempt < max_retries:
                        # Record as failure, back off, and retry with feedback
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        previous_sql = generated_sql
                        error_feedback = str(validation_error)
                        await asyncio.sleep(self._backoff_delay(attempt))
                        continue
                    # Out of retries, record failure and raise
                    self.circuit_breaker.record_failure()
                    metrics.increment_sql_rejected(
                        "security_violation"
                        if isinstance(validation_error, SecurityViolationError)
                        else "validation_failed"
                    )
                    logger.error(
                        "SQL validation failed after all retries",
                        extra={
                            "request_id": request_id,
                            "attempts": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                # Build validation result
                validation_result = ValidationResult(
                    is_valid=True,
                    is_select=True,
                    allows_data_modification=False,
                    uses_blocked_functions=[],
                    error_message=None,
                )

                return generated_sql, validation_result, tokens_used

            except PgMcpError:
                # Re-raise known errors (including rate limit errors)
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation disabled/fails.

        Example:
            >>> confidence = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            metrics.increment_llm_call("validate_result")
            llm_started = time.perf_counter()
            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )
            metrics.observe_llm_latency("validate_result", time.perf_counter() - llm_started)

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence

        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 100  # Default to high confidence if validation fails

    async def _acquire_query_slot(self) -> None:
        """Acquire a query rate-limit slot.

        Raises:
            RateLimitExceededError: If no slot became available within the
                configured ``rate_limit_timeout``.
        """
        if self.rate_limiter is None:
            return
        acquired = await self.rate_limiter.query_limiter.acquire(
            timeout=self.resilience_config.rate_limit_timeout
        )
        if not acquired:
            raise RateLimitExceededError(
                message="Too many concurrent queries, please retry later",
                details={
                    "limit": self.rate_limiter.query_limiter.max_concurrent,
                    "timeout": self.resilience_config.rate_limit_timeout,
                },
            )

    def _release_query_slot(self) -> None:
        """Release the query rate-limit slot (no-op when rate limiting is off)."""
        if self.rate_limiter is not None:
            self.rate_limiter.query_limiter.release()

    @asynccontextmanager
    async def _llm_slot(self) -> AsyncIterator[None]:
        """Rate-limit slot around a single LLM API call.

        Raises:
            RateLimitExceededError: If no slot became available within the
                configured ``rate_limit_timeout``.
        """
        if self.rate_limiter is None:
            yield
            return
        acquired = await self.rate_limiter.llm_limiter.acquire(
            timeout=self.resilience_config.rate_limit_timeout
        )
        if not acquired:
            raise RateLimitExceededError(
                message="LLM rate limit exceeded, please retry later",
                details={
                    "limit": self.rate_limiter.llm_limiter.max_concurrent,
                    "timeout": self.resilience_config.rate_limit_timeout,
                },
            )
        try:
            yield
        finally:
            self.rate_limiter.llm_limiter.release()

    def _backoff_delay(self, attempt: int) -> float:
        """Compute exponential backoff delay before the next retry.

        Args:
            attempt: Zero-based index of the attempt that just failed.

        Returns:
            float: Delay in seconds.
        """
        return self.resilience_config.retry_delay * (
            self.resilience_config.backoff_factor**attempt
        )

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get current time in milliseconds.

        Returns:
            float: Current time in milliseconds since epoch.
        """
        return time.time() * 1000
