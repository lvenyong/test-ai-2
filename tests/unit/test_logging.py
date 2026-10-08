"""Unit tests for structured logging utilities.

Covers the sensitive-data filter, JSON/text formatters, and logging
configuration helpers.
"""

import json
import logging

from pg_mcp.observability.logging import (
    JSONFormatter,
    SensitiveDataFilter,
    TextFormatter,
    configure_logging,
    get_logger,
)


def make_record(msg: str = "hello", *args: object) -> logging.LogRecord:
    """Create a bare log record for filter/formatter tests."""
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=None,
    )


class TestSensitiveDataFilter:
    """Tests for SensitiveDataFilter sanitization."""

    def test_filter_allows_record(self) -> None:
        record = make_record()
        assert SensitiveDataFilter().filter(record) is True

    def test_extra_sensitive_key_redacted(self) -> None:
        record = make_record()
        record.password = "super-secret"
        SensitiveDataFilter().filter(record)
        assert record.password == "***REDACTED***"

    def test_extra_sensitive_key_case_insensitive(self) -> None:
        record = make_record()
        record.API_KEY = "sk-abc"
        SensitiveDataFilter().filter(record)
        assert record.API_KEY == "***REDACTED***"

    def test_nested_dict_sanitized(self) -> None:
        record = make_record()
        record.context = {"user": "alice", "token": "t-123", "nested": {"pwd": "p"}}
        SensitiveDataFilter().filter(record)
        assert record.context["user"] == "alice"
        assert record.context["token"] == "***REDACTED***"
        assert record.context["nested"]["pwd"] == "***REDACTED***"

    def test_args_sanitized(self) -> None:
        record = make_record("connect to %s with %s", "db-host", {"api_key": "k"})
        SensitiveDataFilter().filter(record)
        assert record.args[0] == "db-host"
        assert record.args[1]["api_key"] == "***REDACTED***"

    def test_list_of_dicts_sanitized(self) -> None:
        record = make_record()
        record.items = [{"secret": "s"}, {"name": "n"}]
        SensitiveDataFilter().filter(record)
        assert record.items[0]["secret"] == "***REDACTED***"
        assert record.items[1]["name"] == "n"

    def test_non_sensitive_values_untouched(self) -> None:
        record = make_record()
        record.username = "alice"
        record.count = 7
        SensitiveDataFilter().filter(record)
        assert record.username == "alice"
        assert record.count == 7


class TestJSONFormatter:
    """Tests for JSON log formatting."""

    def test_basic_fields(self) -> None:
        formatted = JSONFormatter().format(make_record("query executed"))
        data = json.loads(formatted)
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "query executed"
        assert data["line"] == 1

    def test_request_id_included(self) -> None:
        record = make_record()
        record.request_id = "req-1"
        data = json.loads(JSONFormatter().format(record))
        assert data["request_id"] == "req-1"

    def test_extra_fields_grouped(self) -> None:
        record = make_record()
        record.database = "clinical_db"
        data = json.loads(JSONFormatter().format(record))
        assert data["extra"]["database"] == "clinical_db"

    def test_exception_included(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = make_record()
            record.exc_info = sys.exc_info()
        data = json.loads(JSONFormatter().format(record))
        assert "ValueError: boom" in data["exception"]


class TestTextFormatter:
    """Tests for human-readable text formatting."""

    def test_basic_format(self) -> None:
        formatted = TextFormatter().format(make_record("query executed"))
        assert "[INFO]" in formatted
        assert "test.logger - query executed" in formatted

    def test_request_id_appended(self) -> None:
        record = make_record()
        record.request_id = "req-9"
        formatted = TextFormatter().format(record)
        assert "[request_id=req-9]" in formatted

    def test_exception_appended(self) -> None:
        import sys

        try:
            raise RuntimeError("bad")
        except RuntimeError:
            record = make_record()
            record.exc_info = sys.exc_info()
        formatted = TextFormatter().format(record)
        assert "RuntimeError: bad" in formatted


class TestConfigureLogging:
    """Tests for logging configuration."""

    def setup_method(self) -> None:
        """Snapshot root logger state so each test can restore it."""
        self._root = logging.getLogger()
        self._handlers = self._root.handlers[:]
        self._level = self._root.level

    def teardown_method(self) -> None:
        """Restore the original root logger state."""
        self._root.handlers[:] = self._handlers
        self._root.setLevel(self._level)

    def test_configure_json_format(self) -> None:
        configure_logging(level="DEBUG", log_format="json")
        assert isinstance(self._root.handlers[0].formatter, JSONFormatter)
        assert self._root.level == logging.DEBUG

    def test_configure_text_format(self) -> None:
        configure_logging(level="INFO", log_format="text")
        assert isinstance(self._root.handlers[0].formatter, TextFormatter)

    def test_sensitive_filter_attached(self) -> None:
        configure_logging(enable_sensitive_filter=True)
        filters = self._root.handlers[0].filters
        assert any(isinstance(f, SensitiveDataFilter) for f in filters)

    def test_no_sensitive_filter_when_disabled(self) -> None:
        configure_logging(enable_sensitive_filter=False)
        filters = self._root.handlers[0].filters
        assert not any(isinstance(f, SensitiveDataFilter) for f in filters)

    def test_third_party_loggers_quieted(self) -> None:
        configure_logging()
        assert logging.getLogger("asyncpg").level == logging.WARNING
        assert logging.getLogger("openai").level == logging.WARNING


def test_get_logger() -> None:
    logger = get_logger("some.module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "some.module"
