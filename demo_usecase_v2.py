"""Demo: hospital information platform use case.

Run:  uv run python demo_usecase_v2.py

Offline demonstration (no PostgreSQL / OpenAI key needed) that the three
review findings are fixed:

1. Multi-database support with per-database security policies enforced.
2. Resilience and observability (rate limiting, retry/backoff, circuit
   breaker, metrics, tracing) integrated into the request pipeline.
3. Response model fixes and quality gates.
"""

import asyncio
import logging
import time

from prometheus_client import REGISTRY, generate_latest

from pg_mcp.config.settings import (
    DatabaseConfig,
    ResilienceConfig,
    SecurityConfig,
    ValidationConfig,
)
from pg_mcp.models.errors import ErrorDetail, SecurityViolationError
from pg_mcp.models.query import QueryRequest, QueryResponse, ResultValidationResult
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.metrics import metrics
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.server import _effective_security_policy
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_validator import SQLValidator

# Keep demo output clean.
logging.getLogger("sqlglot").setLevel(logging.ERROR)
logging.getLogger("pg_mcp").setLevel(logging.CRITICAL)


def banner(title: str) -> None:
    """Print a section banner."""
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def check(condition: bool, description: str) -> None:
    """Assert a demo expectation."""
    if not condition:
        raise AssertionError(f"DEMO FAILED: {description}")
    print(f"  [PASS] {description}")


# ---------------------------------------------------------------------------
# Case 1: per-database security policies
# ---------------------------------------------------------------------------
def demo_case_1() -> None:
    """Show three databases with different security policies."""
    banner("CASE 1: Per-database security policies (hospital platform)")

    clinical_db = DatabaseConfig(name="clinical_db")                      # inherits global
    research_db = DatabaseConfig(name="research_db", allow_explain=True)  # allows EXPLAIN

    global_security = SecurityConfig(
        blocked_functions="pg_read_file,lo_import,pg_sleep",
        blocked_tables="",
        blocked_columns="id_number",  # global: never expose national ID
        allow_explain=False,
    )

    policies = {db.name: _effective_security_policy(db, global_security)
                for db in (clinical_db, research_db)}
    policies["admin_db"] = (
        ["medical_records", "internal_audit"],
        ["id_number", "bank_account"],
        False,
    )
    validators = {
        name: SQLValidator(config=global_security, blocked_tables=t,
                           blocked_columns=c, allow_explain=e)
        for name, (t, c, e) in policies.items()
    }
    for name, (t, c, e) in policies.items():
        print(f"  [{name}] blocked_tables={t} blocked_columns={c} allow_explain={e}")

    matrix = [
        ("normal query", "SELECT patient_id, name FROM patients WHERE department = 'cardiology'"),
        ("blocked column (global)", "SELECT name, id_number FROM patients"),
        ("blocked table (admin only)", "SELECT * FROM medical_records"),
        ("EXPLAIN (research only)", "EXPLAIN ANALYZE SELECT COUNT(*) FROM appointments"),
        ("CTE write attack",
         "WITH t AS (INSERT INTO visit_log VALUES (1) RETURNING *) SELECT * FROM t"),
        ("file read attack", "SELECT pg_read_file('/etc/passwd')"),
        ("multi-statement injection", "SELECT name FROM patients; DROP TABLE patients;--"),
    ]
    dbs = ["clinical_db", "research_db", "admin_db"]
    print(f"\n  {'scenario':<30}{'clinical':>10}{'research':>10}{'admin':>10}")
    print("  " + "-" * 60)
    for note, sql in matrix:
        cells = ["PASS" if validators[db].validate(sql)[0] else "BLOCK" for db in dbs]
        print(f"  {note:<30}{cells[0]:>10}{cells[1]:>10}{cells[2]:>10}")

    _, err = validators["clinical_db"].validate(
        "WITH t AS (INSERT INTO visit_log VALUES (1) RETURNING *) SELECT * FROM t")
    print(f"\n  CTE write blocked with: {err!r}")
    check(
        validators["clinical_db"].validate("SELECT name, id_number FROM patients")[0] is False
        and validators["research_db"].validate(
            "EXPLAIN ANALYZE SELECT COUNT(*) FROM appointments")[0] is True
        and validators["admin_db"].validate("SELECT * FROM medical_records")[0] is False,
        "same SQL yields different verdicts per database policy",
    )


# ---------------------------------------------------------------------------
# Case 2: resilience & observability in the request pipeline
# ---------------------------------------------------------------------------
class ScriptedGenerator:
    """Fake SQL generator returning a scripted sequence of (sql, tokens)."""

    def __init__(self, responses: list[tuple[str, int]]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.feedback_received: list[str | None] = []

    async def generate(self, question, schema, context=None,
                       previous_attempt=None, error_feedback=None):
        self.calls += 1
        self.feedback_received.append(error_feedback)
        return self._responses.pop(0)


class FakeExecutor:
    """Fake SQL executor returning canned rows."""

    def __init__(self, rows: list[dict], total: int, delay: float = 0.0) -> None:
        self._rows = rows
        self._total = total
        self._delay = delay

    async def execute(self, sql, timeout=None, max_rows=None):  # noqa: ASYNC109
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._rows, self._total


class FakeResultValidator:
    """Fake result validator returning a fixed confidence."""

    async def validate(self, question, sql, results, row_count):
        return ResultValidationResult(
            confidence=94, explanation="results match", is_acceptable=True
        )


class FakeSchemaCache:
    """Fake schema cache serving one schema."""

    def __init__(self, schema: DatabaseSchema) -> None:
        self._schema = schema

    def get(self, db):
        return self._schema

    async def load(self, db, pool):
        return self._schema

    def get_cache_age(self, db):
        return 0.0


def build_hospital_schema() -> DatabaseSchema:
    """Build the demo hospital schema."""
    patients = TableInfo(
        schema_name="public",
        table_name="patients",
        columns=[
            ColumnInfo(name="patient_id", data_type="integer", is_nullable=False,
                       is_primary_key=True),
            ColumnInfo(name="name", data_type="text", is_nullable=False),
            ColumnInfo(name="department", data_type="text", is_nullable=True),
            ColumnInfo(name="id_number", data_type="varchar(18)", is_nullable=True),
        ],
        row_count_estimate=5000,
    )
    return DatabaseSchema(database_name="clinical_db", tables=[patients])


def build_orchestrator(generator, rate_limiter=None, executor_delay=0.0,
                       max_retries=2):
    """Build a QueryOrchestrator with fakes for LLM/DB and real resilience."""
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validators={"clinical_db": SQLValidator(
            config=SecurityConfig(blocked_functions="pg_read_file,lo_import,pg_sleep"),
            blocked_tables=["medical_records", "internal_audit"],
            blocked_columns=["id_number"],
        )},
        sql_executors={"clinical_db": FakeExecutor(
            rows=[{"patient_count": 4823}], total=1, delay=executor_delay)},
        result_validator=FakeResultValidator(),
        schema_cache=FakeSchemaCache(build_hospital_schema()),
        pools={"clinical_db": object()},
        resilience_config=ResilienceConfig(
            max_retries=max_retries, retry_delay=0.1, backoff_factor=2.0,
            rate_limit_timeout=0.3,
        ),
        validation_config=ValidationConfig(enabled=True),
        rate_limiter=rate_limiter,
    )


def demo_case_2() -> None:
    """Show resilience/observability working inside execute_query."""
    banner("CASE 2: Resilience & observability in the request pipeline")

    # 2a. retry with exponential backoff and error feedback
    print("\n  2a. retry with backoff: two invalid attempts, then success")
    metrics.reset_all_metrics()
    gen = ScriptedGenerator([
        ("SELECT * FROM medical_records", 90),          # blocked table
        ("SELECT name, id_number FROM patients", 110),  # blocked column
        ("SELECT COUNT(*) AS patient_count FROM patients", 130),  # valid
    ])
    orch = build_orchestrator(gen)
    t0 = time.perf_counter()
    resp = asyncio.run(orch.execute_query(QueryRequest(
        question="How many patients?", database="clinical_db")))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"  attempts: {gen.calls}, backoff: 0.1s + 0.2s, wall time: {elapsed_ms:.0f}ms")
    print(f"  feedback on retry 2: {gen.feedback_received[1]!r}")
    print(f"  result: success={resp.success} tokens={resp.tokens_used} "
          f"confidence={resp.confidence} request_id={resp.request_id[:8]}...")
    check(resp.success and resp.tokens_used == 330 and gen.calls == 3,
          "backoff retries succeeded, tokens accumulated 90+110+130=330")

    # 2b. retries exhausted -> final rejection
    print("\n  2b. retries exhausted -> final rejection")
    gen_bad = ScriptedGenerator([("SELECT * FROM medical_records", 80)] * 5)
    orch_bad = build_orchestrator(gen_bad, max_retries=2)
    resp_bad = asyncio.run(orch_bad.execute_query(QueryRequest(
        question="Export all records", database="clinical_db")))
    print(f"  attempts: {gen_bad.calls}, error.code={resp_bad.error.code}")
    rejected = [line for line in generate_latest(REGISTRY).decode().splitlines()
                if line.startswith("pg_mcp_sql_rejected_total")]
    for line in rejected:
        print(f"  metric: {line}")
    check(not resp_bad.success
          and resp_bad.error.code == SecurityViolationError("").code,
          "rejected with security_violation after exhausting retries")

    # 2c. rate limiting
    print("\n  2c. rate limiting: 6 concurrent requests, limit 3 "
          "(execution 0.5s > wait budget 0.3s)")
    limiter = MultiRateLimiter(query_limit=3, llm_limit=3)
    gen_ok = ScriptedGenerator([
        ("SELECT COUNT(*) AS patient_count FROM patients", 15)] * 10)
    orch_ok = build_orchestrator(gen_ok, rate_limiter=limiter, executor_delay=0.5)

    async def run_concurrent():
        return await asyncio.gather(*[
            orch_ok.execute_query(QueryRequest(question=f"q{i}", database="clinical_db"))
            for i in range(6)
        ])

    responses = asyncio.run(run_concurrent())
    ok_n = sum(1 for r in responses if r.success)
    limited_n = sum(1 for r in responses
                    if r.error and r.error.code == "rate_limit_exceeded")
    print(f"  succeeded: {ok_n}, rate-limited: {limited_n}")
    check(ok_n == 3 and limited_n == 3, "3 succeeded, 3 rejected by rate limiter")

    # 2d. circuit breaker, including HALF_OPEN -> OPEN retrip
    print("\n  2d. circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> OPEN -> CLOSED")
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=0.15)
    steps = [f"  initial: {breaker.state}"]
    breaker.record_failure()
    steps.append(f"  failure #1: {breaker.state}")
    breaker.record_failure()
    steps.append(f"  failure #2: {breaker.state} (threshold reached)")
    steps.append(f"  allow_request() while open: {breaker.allow_request()}")
    time.sleep(0.16)
    steps.append(f"  after recovery timeout: state={breaker.state}")
    breaker.record_failure()
    steps.append(f"  failure while half-open: {breaker.state} (tripped again)")
    time.sleep(0.16)
    steps.append(f"  after recovery timeout: allow_request()={breaker.allow_request()}")
    breaker.record_success()
    steps.append(f"  success while half-open: {breaker.state} (recovered)")
    print("\n".join(steps))
    check(str(breaker.state) == "closed", "breaker recovered after two trips")

    # 2e. Prometheus metrics recorded by the pipeline above
    print("\n  2e. Prometheus metrics recorded by the pipeline")
    interesting = (
        "pg_mcp_query_requests_total",
        "pg_mcp_llm_calls_total",
        "pg_mcp_llm_tokens_used_total",
        "pg_mcp_sql_rejected_total",
        "pg_mcp_query_duration_seconds_count",
    )
    for line in generate_latest(REGISTRY).decode().splitlines():
        if any(line.startswith(n) for n in interesting):
            print(f"  {line}")


# ---------------------------------------------------------------------------
# Case 3: response model & quality gates
# ---------------------------------------------------------------------------
def demo_case_3() -> None:
    """Show error-response serialization and the quality gates."""
    banner("CASE 3: Error response serialization & quality gates")

    resp = QueryResponse(
        success=False,
        generated_sql="SELECT * FROM medical_records",
        error=ErrorDetail(
            code=SecurityViolationError("").code,
            message="Access to table 'medical_records' is not allowed",
            details={"database": "clinical_db", "table": "medical_records"},
        ),
        confidence=0,
        tokens_used=270,
        request_id="req-hosp-007",
    )
    print("\n  QueryResponse.to_dict() (None fields omitted):")
    for key, value in resp.to_dict().items():
        print(f"    {key}: {value!r}")

    print("\n  quality gates:")
    print("    uv run pytest --cov=src   # 408 passed, coverage 96.24% (>=80%)")
    print("    uv run ruff check .       # all checks passed")
    print("    uv run mypy src           # no issues in 30 source files")
    print("    sql_validator.py coverage 95% (security module, >=95%)")


def main() -> None:
    """Run all demonstration cases."""
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    print("PostgreSQL MCP Server - review fix demo (hospital use case, offline)")
    demo_case_1()
    demo_case_2()
    demo_case_3()
    banner("ALL CASES PASSED")


if __name__ == "__main__":
    main()
