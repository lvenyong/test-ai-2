"""Unit tests for request tracing and context propagation."""

import logging
import uuid

import pytest

from pg_mcp.observability.tracing import (
    TraceContext,
    TracingLogger,
    clear_request_id,
    generate_request_id,
    get_request_id,
    get_tracing_logger,
    request_context,
    set_request_id,
    trace_async,
    trace_sync,
)


class TestRequestIdHelpers:
    """Tests for request ID generation and context variables."""

    def test_generate_request_id_is_uuid(self) -> None:
        request_id = generate_request_id()
        assert uuid.UUID(request_id)  # parses as a UUID
        assert generate_request_id() != request_id

    def test_default_request_id_is_none(self) -> None:
        clear_request_id()
        assert get_request_id() is None

    def test_set_and_clear_request_id(self) -> None:
        set_request_id("custom-id")
        assert get_request_id() == "custom-id"
        clear_request_id()
        assert get_request_id() is None

    async def test_request_context_yields_and_resets(self) -> None:
        clear_request_id()
        async with request_context() as request_id:
            assert request_id
            assert get_request_id() == request_id
        assert get_request_id() is None

    async def test_request_context_explicit_id(self) -> None:
        async with request_context("fixed-id") as request_id:
            assert request_id == "fixed-id"
            assert get_request_id() == "fixed-id"

    async def test_nested_request_contexts(self) -> None:
        async with request_context("outer") as outer:
            async with request_context("inner") as inner:
                assert inner == "inner"
                assert get_request_id() == "inner"
            assert get_request_id() == outer

    async def test_context_restored_after_exception(self) -> None:
        set_request_id("before")
        with pytest.raises(RuntimeError, match="x"):
            async with request_context("during"):
                raise RuntimeError("x")
        assert get_request_id() == "before"


class TestTraceDecorators:
    """Tests for trace_async and trace_sync decorators."""

    async def test_trace_async_propagates_request_id(self) -> None:
        @trace_async(operation="op")
        async def work() -> str:
            return "ok"

        async with request_context("req-1"):
            assert await work() == "ok"
        # Factory restored afterwards
        assert logging.getLogRecordFactory() is logging.LogRecord

    async def test_trace_async_records_request_id(self) -> None:
        @trace_async(operation="gen_sql")
        async def work() -> None:
            factory = logging.getLogRecordFactory()
            record = factory("t", logging.INFO, __file__, 1, "msg", None, None)
            assert record.request_id == "req-2"
            assert record.operation == "gen_sql"

        async with request_context("req-2"):
            await work()
        assert logging.getLogRecordFactory() is logging.LogRecord

    async def test_trace_async_restores_factory_on_error(self) -> None:
        @trace_async()
        async def boom() -> None:
            raise ValueError("fail")

        async with request_context("req-3"):
            with pytest.raises(ValueError, match="fail"):
                await boom()
        assert logging.getLogRecordFactory() is logging.LogRecord

    async def test_trace_async_without_context(self) -> None:
        clear_request_id()

        @trace_async()
        async def work() -> str:
            return "done"

        assert await work() == "done"

    def test_trace_sync_records_request_id(self) -> None:
        @trace_sync(operation="validate")
        def work() -> None:
            factory = logging.getLogRecordFactory()
            record = factory("t", logging.INFO, __file__, 1, "msg", None, None)
            assert record.request_id == "req-4"
            assert record.operation == "validate"

        set_request_id("req-4")
        try:
            work()
            assert logging.getLogRecordFactory() is logging.LogRecord
        finally:
            clear_request_id()

    def test_trace_sync_without_context(self) -> None:
        clear_request_id()

        @trace_sync()
        def work() -> int:
            return 5

        assert work() == 5

    def test_trace_sync_restores_factory_on_error(self) -> None:
        @trace_sync()
        def boom() -> None:
            raise ValueError("sync fail")

        set_request_id("req-5")
        try:
            with pytest.raises(ValueError, match="sync fail"):
                boom()
            assert logging.getLogRecordFactory() is logging.LogRecord
        finally:
            clear_request_id()


class TestTracingLogger:
    """Tests for the TracingLogger wrapper."""

    def test_log_levels_add_request_id(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("trace.test")
        set_request_id("req-t")
        try:
            with caplog.at_level(logging.DEBUG, logger="trace.test"):
                logger.debug("d")
                logger.info("i")
                logger.warning("w")
                logger.error("e")
                logger.critical("c")
        finally:
            clear_request_id()
        request_ids = [r.request_id for r in caplog.records]
        assert request_ids == ["req-t"] * 5

    def test_existing_extra_preserved(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("trace.test2")
        set_request_id("req-x")
        try:
            with caplog.at_level(logging.INFO, logger="trace.test2"):
                logger.info("msg", extra={"database": "db1", "request_id": "kept"})
        finally:
            clear_request_id()
        record = caplog.records[0]
        assert record.request_id == "kept"
        assert record.database == "db1"

    def test_exception_logs_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = TracingLogger("trace.test3")
        with caplog.at_level(logging.ERROR, logger="trace.test3"):
            try:
                raise ValueError("traceback expected")
            except ValueError:
                logger.exception("caught")
        assert caplog.records[0].exc_info is not None


def test_trace_context_model() -> None:
    context = TraceContext(
        request_id="r1", parent_id="r0", operation="op", metadata={"k": "v"}
    )
    assert context.request_id == "r1"
    assert context.parent_id == "r0"
    assert context.metadata == {"k": "v"}


def test_trace_context_defaults() -> None:
    context = TraceContext(request_id="r1")
    assert context.parent_id is None
    assert context.operation is None
    assert context.metadata is None
