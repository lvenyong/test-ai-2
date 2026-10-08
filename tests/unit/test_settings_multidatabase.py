"""Unit tests for multi-database configuration.

This module covers the EXTRA_DATABASES JSON parsing, the all_databases
merge logic, and the per-database security policy overrides.
"""

import pytest
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from pg_mcp.config.settings import (
    DatabaseConfig,
    OpenAIConfig,
    ResilienceConfig,
    Settings,
)


@pytest.fixture
def openai_config() -> OpenAIConfig:
    """Provide a valid OpenAI config for Settings construction."""
    return OpenAIConfig(api_key="sk-test")


class TestExtraDatabases:
    """Tests for EXTRA_DATABASES JSON parsing."""

    def test_parse_from_env_json(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test parsing extra databases from a JSON environment variable."""
        monkeypatch.setenv(
            "EXTRA_DATABASES",
            '{"analytics": {"host": "db2.internal", "port": 5433, "name": "analytics"}}',
        )
        settings = Settings(openai=openai_config)

        assert "analytics" in settings.extra_databases
        extra = settings.extra_databases["analytics"]
        assert extra.host == "db2.internal"
        assert extra.port == 5433

    def test_name_synced_to_dict_key(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test an entry's name is forced to match its dict key."""
        monkeypatch.setenv(
            "EXTRA_DATABASES",
            '{"analytics": {"name": "ignored_name"}}',
        )
        settings = Settings(openai=openai_config)

        assert settings.extra_databases["analytics"].name == "analytics"

    def test_missing_keys_fall_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test absent JSON keys fall back to DATABASE_* env values / defaults."""
        monkeypatch.setenv("DATABASE_HOST", "primary-host")
        monkeypatch.setenv(
            "EXTRA_DATABASES",
            '{"analytics": {}}',
        )
        settings = Settings(openai=openai_config)

        assert settings.extra_databases["analytics"].host == "primary-host"

    def test_invalid_json_rejected(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test invalid JSON raises an error."""
        monkeypatch.setenv("EXTRA_DATABASES", "{not valid json")
        with pytest.raises(SettingsError):
            Settings(openai=openai_config)

    def test_non_object_json_rejected(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test a JSON array is rejected."""
        monkeypatch.setenv("EXTRA_DATABASES", '["analytics"]')
        with pytest.raises(ValidationError):
            Settings(openai=openai_config)

    def test_non_object_entry_rejected(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test a non-object entry is rejected."""
        monkeypatch.setenv("EXTRA_DATABASES", '{"analytics": "not-a-dict"}')
        with pytest.raises(ValidationError):
            Settings(openai=openai_config)

    def test_empty_by_default(self, openai_config: OpenAIConfig) -> None:
        """Test extra_databases is empty when the env var is absent."""
        settings = Settings(openai=openai_config)
        assert settings.extra_databases == {}


class TestAllDatabases:
    """Tests for the all_databases merge property."""

    def test_primary_only(self, openai_config: OpenAIConfig) -> None:
        """Test all_databases contains only the primary by default."""
        settings = Settings(openai=openai_config)
        assert list(settings.all_databases.keys()) == [settings.database.name]

    def test_primary_plus_extras(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test primary and extra databases are merged."""
        monkeypatch.setenv("DATABASE_NAME", "main")
        monkeypatch.setenv(
            "EXTRA_DATABASES",
            '{"analytics": {"name": "analytics"}, "reporting": {"name": "reporting"}}',
        )
        settings = Settings(openai=openai_config)

        assert set(settings.all_databases.keys()) == {"main", "analytics", "reporting"}

    def test_primary_wins_on_name_collision(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test an extra database colliding with the primary name is dropped."""
        monkeypatch.setenv("DATABASE_NAME", "main")
        monkeypatch.setenv(
            "EXTRA_DATABASES",
            '{"main": {"host": "evil.host"}}',
        )
        settings = Settings(openai=openai_config)

        assert set(settings.all_databases.keys()) == {"main"}
        assert settings.all_databases["main"].host != "evil.host"


class TestDatabaseSecurityOverrides:
    """Tests for per-database security policy override fields."""

    def test_overrides_default_to_none(self) -> None:
        """Test the override fields default to None (inherit global)."""
        config = DatabaseConfig()
        assert config.blocked_tables is None
        assert config.blocked_columns is None
        assert config.allow_explain is None

    def test_overrides_from_constructor(self) -> None:
        """Test override fields set via constructor."""
        config = DatabaseConfig(
            blocked_tables=["secrets"],
            blocked_columns=["users.ssn"],
            allow_explain=True,
        )
        assert config.blocked_tables == ["secrets"]
        assert config.blocked_columns == ["users.ssn"]
        assert config.allow_explain is True

    def test_overrides_parsed_from_env_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test override fields parse comma-separated environment variables."""
        monkeypatch.setenv("DATABASE_BLOCKED_TABLES", "t1, t2")
        monkeypatch.setenv("DATABASE_BLOCKED_COLUMNS", "c1, users.c2")
        monkeypatch.setenv("DATABASE_ALLOW_EXPLAIN", "true")

        config = DatabaseConfig()
        assert config.blocked_tables == ["t1", "t2"]
        assert config.blocked_columns == ["c1", "users.c2"]
        assert config.allow_explain is True

    def test_override_via_extra_databases_json(
        self, monkeypatch: pytest.MonkeyPatch, openai_config: OpenAIConfig
    ) -> None:
        """Test per-DB security overrides inside EXTRA_DATABASES JSON."""
        monkeypatch.setenv(
            "EXTRA_DATABASES",
            '{"analytics": {"blocked_tables": ["raw_events"], "allow_explain": false}}',
        )
        settings = Settings(openai=openai_config)

        extra = settings.extra_databases["analytics"]
        assert extra.blocked_tables == ["raw_events"]
        assert extra.allow_explain is False


class TestResilienceRateLimitConfig:
    """Tests for the new rate limit configuration fields."""

    def test_defaults(self) -> None:
        """Test rate limit defaults match the previously hardcoded values."""
        config = ResilienceConfig()
        assert config.query_rate_limit == 10
        assert config.llm_rate_limit == 5
        assert config.rate_limit_timeout == 30.0

    def test_invalid_values_rejected(self) -> None:
        """Test out-of-range rate limit values are rejected."""
        with pytest.raises(ValidationError):
            ResilienceConfig(query_rate_limit=0)

        with pytest.raises(ValidationError):
            ResilienceConfig(rate_limit_timeout=0.0)
