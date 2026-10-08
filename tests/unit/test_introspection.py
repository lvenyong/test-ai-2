"""Unit tests for PostgreSQL schema introspection.

A routing fake connection stands in for asyncpg, returning canned catalog
rows keyed by recognizable query fragments.
"""

from pg_mcp.db.introspection import SchemaIntrospector

POSTGRES_VERSION = "PostgreSQL 16.2 (Debian), 64-bit"

# --- canned catalog rows ---------------------------------------------------

TABLE_ROWS = [
    {"schema_name": "public", "table_name": "patients", "comment": "患者表"},
    {"schema_name": "public", "table_name": "visits", "comment": None},
]

VIEW_ROWS = [
    {"schema_name": "public", "table_name": "active_patients", "comment": None},
]

COLUMN_ROWS = {
    "patients": [
        {"column_name": "patient_id", "data_type": "integer",
         "is_nullable": False, "default_value": None, "comment": "主键"},
        {"column_name": "name", "data_type": "text",
         "is_nullable": False, "default_value": None, "comment": None},
    ],
    "visits": [
        {"column_name": "visit_id", "data_type": "integer",
         "is_nullable": False, "default_value": None, "comment": None},
    ],
    "active_patients": [
        {"column_name": "patient_id", "data_type": "integer",
         "is_nullable": True, "default_value": None, "comment": None},
    ],
}

PK_ROWS = {
    "patients": [{"column_name": "patient_id"}],
    "visits": [{"column_name": "visit_id"}],
    "active_patients": [],
}

FK_ROWS = {
    "visits": [
        {
            "constraint_name": "fk_visits_patient",
            "column_name": "visit_id",
            "referenced_table": "patients",
            "referenced_column": "patient_id",
        },
    ],
}

INDEX_ROWS = {
    "patients": [
        {
            "index_name": "idx_patients_name",
            "is_unique": False,
            "index_type": "btree",
            "columns": ["name"],
        },
    ],
}

ENUM_ROWS = [
    {
        "schema_name": "public",
        "type_name": "visit_status",
        "values": ["open", "closed"],
    },
]


class FakeConnection:
    """Routes catalog queries to canned rows by recognizable fragments."""

    async def fetchval(self, query: str, *args: object) -> object:
        if "version()" in query:
            return POSTGRES_VERSION
        if "contype = 'u'" in query:
            return True  # name column has a unique constraint
        if "reltuples" in query:
            return 1234
        return None

    async def fetch(self, query: str, *args: object) -> list[dict]:
        if "relkind = 'r'" in query:
            return TABLE_ROWS
        if "relkind = 'v'" in query:
            return VIEW_ROWS
        if "typtype = 'e'" in query:
            return ENUM_ROWS
        if "pg_get_expr" in query:
            table = args[0]
            return COLUMN_ROWS.get(str(table), [])
        if "pg_am" in query:
            return INDEX_ROWS.get(str(args[0]), [])
        if "indisprimary" in query:
            return PK_ROWS.get(str(args[0]), [])
        if "contype = 'f'" in query:
            return FK_ROWS.get(str(args[0]), [])
        raise AssertionError(f"Unexpected query in fake connection: {query[:60]}")


class FakePool:
    """Async context-manager pool yielding the fake connection."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def acquire(self):
        return self


    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class TestSchemaIntrospection:
    """End-to-end introspection over the fake catalog."""

    async def test_introspect_full_schema(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        assert schema.database_name == "clinical_db"
        assert schema.version == "PostgreSQL 16.2 (Debian)"
        names = {t.table_name for t in schema.tables}
        assert names == {"patients", "visits", "active_patients"}

    async def test_primary_keys_marked(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        patients = next(t for t in schema.tables if t.table_name == "patients")
        assert patients.columns[0].name == "patient_id"
        assert patients.columns[0].is_primary_key is True
        assert patients.columns[1].is_primary_key is False

    async def test_unique_constraint_detected(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        patients = next(t for t in schema.tables if t.table_name == "patients")
        assert patients.columns[1].is_unique is True

    async def test_foreign_keys_and_indexes_attached(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        visits = next(t for t in schema.tables if t.table_name == "visits")
        assert visits.foreign_keys[0].referenced_table == "patients"
        assert visits.foreign_keys[0].constraint_name == "fk_visits_patient"

        patients = next(t for t in schema.tables if t.table_name == "patients")
        assert patients.indexes[0].name == "idx_patients_name"
        assert patients.indexes[0].columns == ["name"]

    async def test_row_count_estimate_and_comment(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        patients = next(t for t in schema.tables if t.table_name == "patients")
        assert patients.row_count_estimate == 1234
        assert patients.comment == "患者表"

    async def test_enum_types_loaded(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        assert schema.enum_types[0].type_name == "visit_status"
        assert schema.enum_types[0].values == ["open", "closed"]

    async def test_view_columns_enriched(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        schema = await introspector.introspect()

        view = next(t for t in schema.tables if t.table_name == "active_patients")
        assert view.columns[0].name == "patient_id"
        assert view.foreign_keys == []
        assert view.indexes == []

    async def test_unknown_table_returns_empty_metadata(self) -> None:
        introspector = SchemaIntrospector(FakePool(FakeConnection()), "clinical_db")
        columns = await introspector._get_columns(
            FakeConnection(), "missing_table", "public"
        )
        assert columns == []
