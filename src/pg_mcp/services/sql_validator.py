"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

import re
from typing import ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError

# Matches an EXPLAIN prefix with optional options, e.g.:
#   EXPLAIN SELECT ..., EXPLAIN ANALYZE SELECT ..., EXPLAIN (ANALYZE, BUFFERS) SELECT ...
_EXPLAIN_PREFIX_RE = re.compile(
    r"(?is)^\s*EXPLAIN\s+(?:\(.*?\)\s+|ANALYZE\s+|ANALYSE\s+|VERBOSE\s+)*"
)


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {
        exp.Select, exp.Union, exp.Intersect, exp.Except
    }

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With, exp.Subquery
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool = False,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Optional list of table names to block access to.
            blocked_columns: Optional list of column names to block access to.
            allow_explain: Whether to allow EXPLAIN statements.
        """
        self.config = config
        self.blocked_tables = {t.lower() for t in (blocked_tables or [])}
        self.blocked_columns = {c.lower() for c in (blocked_columns or [])}
        self.allow_explain = allow_explain

        # Bare entries ("secrets") block the table in any schema; qualified
        # entries ("public.secrets") block only that exact schema-qualified name.
        self._bare_blocked_tables = {t for t in self.blocked_tables if "." not in t}
        self._qualified_blocked_tables = {t for t in self.blocked_tables if "." in t}

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:  # pragma: no cover - sqlglot.parse yields [None], never []
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None or isinstance(statement, type(None)):
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements (parsed as Command in sqlglot 28.5.0)
        if isinstance(statement, exp.Command):
            # Check if it's an EXPLAIN command
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                if not self.allow_explain:
                    raise SecurityViolationError("EXPLAIN statements are not allowed")
                # sqlglot cannot parse EXPLAIN syntax reliably (falls back to Command),
                # so strip the EXPLAIN prefix and validate the inner statement with the
                # full rule set. EXPLAIN ANALYZE actually executes the inner statement,
                # so blocked tables/functions must not be able to hide behind EXPLAIN.
                statement_sql = statement.sql(dialect="postgres")
                inner_sql = _EXPLAIN_PREFIX_RE.sub("", statement_sql, count=1).strip()
                if not inner_sql:  # pragma: no cover - sqlglot always re-renders inner SQL
                    raise SQLParseError("EXPLAIN statement has no inner query")
                if re.match(r"(?is)^EXPLAIN\b", inner_sql):
                    raise SecurityViolationError("Nested EXPLAIN statements are not allowed")
                self.validate_or_raise(inner_sql)
                return None
            else:
                # Other commands are not allowed
                raise SecurityViolationError(
                    f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
                )

        # Handle CTE (WITH) statements - extract the main query
        if isinstance(statement, exp.With):
            # WITH statements are allowed, but we need to validate the main query
            if statement.this:
                main_query = statement.this
            # Defensive: sqlglot fails to parse a mainless WITH before we get here.
            else:  # pragma: no cover
                raise SQLParseError("WITH statement has no main query")
        else:
            main_query = statement

        # Perform security checks
        if error := self._check_statement_type(main_query):
            raise SecurityViolationError(error)

        if error := self._check_cte_safety(statement):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Check for forbidden statement types
        for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_cte_safety(self, statement: exp.Expression) -> str | None:
        """Check that all CTE bodies contain only read-only SELECT statements.

        PostgreSQL allows data-modifying statements inside CTEs (e.g.
        ``WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d``), so a
        top-level SELECT alone does not guarantee the query is read-only.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for cte in statement.find_all(exp.CTE):
            body = cte.this
            if body is None:  # pragma: no cover - defensive; parsed CTEs always have a body
                return "CTE has no query body"

            for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                if isinstance(body, forbidden_type):
                    stmt_name = forbidden_type.__name__.upper()
                    return f"{stmt_name} statements in CTEs are not allowed"

            if not isinstance(body, (*self.ALLOWED_STATEMENT_TYPES, exp.With)):
                return "CTEs must contain only SELECT statements"

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""

            # Bare entry matches the table in any schema
            if table_name in self._bare_blocked_tables:
                return f"Access to table '{table_name}' is not allowed"

            # Qualified entry matches the exact schema-qualified name
            if table.db:
                qualified_name = f"{table.db.lower()}.{table_name}"
                if qualified_name in self._qualified_blocked_tables:
                    return f"Access to table '{qualified_name}' is not allowed"

        return None

    def _check_blocked_columns(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked columns.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        # Find all column references
        for column in statement.find_all(exp.Column):
            column_name = column.name.lower() if column.name else ""

            # Check for exact match
            if column_name in self.blocked_columns:
                return f"Access to column '{column_name}' is not allowed"

            # Check for qualified column names (table.column)
            if column.table:
                qualified_name = f"{column.table.lower()}.{column_name}"
                if qualified_name in self.blocked_columns:
                    return f"Access to column '{qualified_name}' is not allowed"

        return None

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all subqueries
        for subquery in statement.find_all(exp.Subquery):
            inner_stmt = subquery.this
            if not inner_stmt:  # pragma: no cover - defensive; sqlglot always sets .this
                continue

            # Unwrap nested parenthesized subqueries (((SELECT ...))) down to
            # the underlying statement, so double parentheses don't cause a
            # false "not a SELECT" rejection.
            while isinstance(inner_stmt, exp.Subquery) and inner_stmt.this is not None:
                inner_stmt = inner_stmt.this

            # Check if the inner statement is a forbidden type
            for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                # Unparseable via sqlglot; guards against future parser changes.
                if isinstance(inner_stmt, forbidden_type):  # pragma: no cover
                    stmt_name = forbidden_type.__name__.upper()
                    return f"{stmt_name} statements in subqueries are not allowed"

            # Ensure it's a read-only statement (SELECT, set operations,
            # WITH, or a VALUES row constructor)
            if not isinstance(
                inner_stmt, (*self.ALLOWED_STATEMENT_TYPES, exp.With, exp.Values)
            ):
                return "Subqueries must contain only SELECT statements"

        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e
