"""Unit tests for the Prometheus metrics collector.

Verifies each collector method records into the default registry, that the
``enabled`` flag silences recording, and that ``reset_all_metrics`` can
rebuild collectors without "Duplicated timeseries" errors.
"""

import pytest
from prometheus_client import REGISTRY


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Read a sample value from the default registry (0.0 when absent)."""
    value = REGISTRY.get_sample_value(name, labels)
    return value if value is not None else 0.0


@pytest.fixture
def recording() -> None:
    """Force metrics recording on for the duration of a test."""
    from pg_mcp.observability.metrics import metrics

    previous = metrics.enabled
    metrics.enabled = True
    yield
    metrics.enabled = previous


class TestCounterMethods:
    """Tests for counter-style recording methods."""

    def test_increment_query_request(self, recording: None) -> None:
        """Test query request counter increments per status/database label."""
        from pg_mcp.observability.metrics import metrics

        name = "pg_mcp_query_requests_total"
        labels = {"status": "success", "database": "metrics_db"}
        before = _sample(name, labels)

        metrics.increment_query_request("success", "metrics_db")

        assert _sample(name, labels) - before == 1.0

    def test_increment_llm_call(self, recording: None) -> None:
        """Test LLM call counter increments per operation label."""
        from pg_mcp.observability.metrics import metrics

        name = "pg_mcp_llm_calls_total"
        labels = {"operation": "generate_sql"}
        before = _sample(name, labels)

        metrics.increment_llm_call("generate_sql")

        assert _sample(name, labels) - before == 1.0

    def test_increment_llm_tokens(self, recording: None) -> None:
        """Test token usage counter accumulates the provided amount."""
        from pg_mcp.observability.metrics import metrics

        name = "pg_mcp_llm_tokens_used_total"
        labels = {"operation": "generate_sql"}
        before = _sample(name, labels)

        metrics.increment_llm_tokens("generate_sql", 42)

        assert _sample(name, labels) - before == 42.0

    def test_increment_sql_rejected(self, recording: None) -> None:
        """Test SQL rejection counter increments per reason label."""
        from pg_mcp.observability.metrics import metrics

        name = "pg_mcp_sql_rejected_total"
        labels = {"reason": "security_violation"}
        before = _sample(name, labels)

        metrics.increment_sql_rejected("security_violation")

        assert _sample(name, labels) - before == 1.0


class TestHistogramMethods:
    """Tests for histogram-style recording methods."""

    def test_observe_llm_latency(self, recording: None) -> None:
        """Test LLM latency histogram records count and sum."""
        from pg_mcp.observability.metrics import metrics

        labels = {"operation": "generate_sql"}
        count_before = _sample("pg_mcp_llm_latency_seconds_count", labels)
        sum_before = _sample("pg_mcp_llm_latency_seconds_sum", labels)

        metrics.observe_llm_latency("generate_sql", 1.5)

        assert _sample("pg_mcp_llm_latency_seconds_count", labels) - count_before == 1.0
        assert _sample("pg_mcp_llm_latency_seconds_sum", labels) - sum_before == 1.5

    def test_observe_db_query_duration(self, recording: None) -> None:
        """Test DB query duration histogram records count and sum."""
        from pg_mcp.observability.metrics import metrics

        count_before = _sample("pg_mcp_db_query_duration_seconds_count")
        sum_before = _sample("pg_mcp_db_query_duration_seconds_sum")

        metrics.observe_db_query_duration(0.25)

        assert _sample("pg_mcp_db_query_duration_seconds_count") - count_before == 1.0
        assert _sample("pg_mcp_db_query_duration_seconds_sum") - sum_before == 0.25

    def test_query_duration_time_context_manager(self, recording: None) -> None:
        """Test the query duration histogram's time() context manager records."""
        from pg_mcp.observability.metrics import metrics

        count_before = _sample("pg_mcp_query_duration_seconds_count")

        with metrics.query_duration.time():
            pass

        assert _sample("pg_mcp_query_duration_seconds_count") - count_before == 1.0


class TestGaugeMethods:
    """Tests for gauge-style recording methods."""

    def test_set_db_connections_active(self, recording: None) -> None:
        """Test active connection gauge is set per database."""
        from pg_mcp.observability.metrics import metrics

        metrics.set_db_connections_active("gauge_db", 7)

        assert _sample("pg_mcp_db_connections_active", {"database": "gauge_db"}) == 7.0

    def test_set_schema_cache_age(self, recording: None) -> None:
        """Test schema cache age gauge is set per database."""
        from pg_mcp.observability.metrics import metrics

        metrics.set_schema_cache_age("gauge_db", 12.5)

        assert _sample("pg_mcp_schema_cache_age_seconds", {"database": "gauge_db"}) == 12.5


class TestEnabledFlag:
    """Tests for the enabled/disabled recording switch."""

    def test_disabled_metrics_are_noop(self) -> None:
        """Test no samples change while recording is disabled."""
        from pg_mcp.observability.metrics import metrics

        assert metrics.enabled is False  # conftest disables metrics for tests

        name = "pg_mcp_query_requests_total"
        labels = {"status": "noop", "database": "noop_db"}
        before = _sample(name, labels)

        metrics.increment_query_request("noop", "noop_db")
        metrics.increment_llm_call("noop")
        metrics.increment_llm_tokens("noop", 10)
        metrics.increment_sql_rejected("noop")
        metrics.observe_llm_latency("noop", 1.0)
        metrics.observe_db_query_duration(1.0)
        metrics.set_db_connections_active("noop_db", 5)
        metrics.set_schema_cache_age("noop_db", 5.0)

        assert _sample(name, labels) == before
        assert _sample("pg_mcp_llm_calls_total", {"operation": "noop"}) == 0.0
        assert _sample("pg_mcp_llm_tokens_used_total", {"operation": "noop"}) == 0.0
        assert _sample("pg_mcp_sql_rejected_total", {"reason": "noop"}) == 0.0


class TestResetMetrics:
    """Tests for reset_all_metrics."""

    def test_reset_all_metrics_clears_and_rebuilds(self, recording: None) -> None:
        """Test reset clears recorded values and keeps collectors usable."""
        from pg_mcp.observability.metrics import metrics

        metrics.increment_query_request("reset", "reset_db")
        metrics.set_db_connections_active("reset_db", 9)
        labels = {"status": "reset", "database": "reset_db"}
        assert _sample("pg_mcp_query_requests_total", labels) == 1.0

        metrics.reset_all_metrics()

        # Labeled samples are gone until new labels are used.
        assert REGISTRY.get_sample_value("pg_mcp_query_requests_total", labels) is None

        # Rebuilt collectors still record.
        metrics.increment_query_request("after_reset", "reset_db")
        after_labels = {"status": "after_reset", "database": "reset_db"}
        assert _sample("pg_mcp_query_requests_total", after_labels) == 1.0
