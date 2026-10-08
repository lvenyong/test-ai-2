"""Unit tests for the LLM-based result validation service.

The OpenAI client is mocked; tests cover response parsing, error mapping,
and prompt construction.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from pg_mcp.config.settings import OpenAIConfig, ValidationConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from pg_mcp.prompts.result_validation import build_validation_prompt
from pg_mcp.services.result_validator import ResultValidator


def make_validator(**validation_overrides) -> ResultValidator:
    """Create a ResultValidator with test config."""
    openai_config = OpenAIConfig(api_key=SecretStr("sk-test-key-12345"))
    validation_config = ValidationConfig(**validation_overrides)
    return ResultValidator(openai_config=openai_config, validation_config=validation_config)


def make_response(content: str | None, *, empty_choices: bool = False):
    """Build a fake ChatCompletion response."""
    response = MagicMock()
    if empty_choices:
        response.choices = []
        return response
    choice = MagicMock()
    choice.message.content = content
    response.choices = [choice]
    return response


class TestValidateDisabled:
    """Behavior when result validation is disabled."""

    async def test_disabled_returns_full_confidence(self) -> None:
        validator = make_validator(enabled=False)
        result = await validator.validate(
            question="q", sql="SELECT 1", results=[], row_count=0
        )
        assert result.confidence == 100
        assert result.is_acceptable is True
        assert result.suggestion is None


class TestValidateSuccess:
    """Successful LLM round-trips."""

    async def test_valid_json_response(self) -> None:
        validator = make_validator()
        payload = {
            "confidence": 85,
            "explanation": "results match",
            "suggestion": "add an index",
        }
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(json.dumps(payload))
        )

        result = await validator.validate(
            question="how many?",
            sql="SELECT COUNT(*) FROM t",
            results=[{"count": 5}],
            row_count=1,
        )
        assert result.confidence == 85
        assert result.explanation == "results match"
        assert result.suggestion == "add an index"
        assert result.is_acceptable is True

    async def test_below_threshold_not_acceptable(self) -> None:
        validator = make_validator(confidence_threshold=90)
        payload = {"confidence": 40, "explanation": "mismatch", "suggestion": None}
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(json.dumps(payload))
        )
        result = await validator.validate("q", "SELECT 1", [], 0)
        assert result.confidence == 40
        assert result.is_acceptable is False

    async def test_missing_fields_use_defaults(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(json.dumps({}))
        )
        result = await validator.validate("q", "SELECT 1", [], 0)
        assert result.confidence == 50
        assert result.explanation == "No explanation provided"
        assert result.suggestion is None

    async def test_confidence_clamped_to_bounds(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(json.dumps({"confidence": 150}))
        )
        assert (await validator.validate("q", "s", [], 0)).confidence == 100

    async def test_confidence_negative_clamped(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(json.dumps({"confidence": -5}))
        )
        assert (await validator.validate("q", "s", [], 0)).confidence == 0

    async def test_confidence_non_numeric_defaults(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(
                json.dumps({"confidence": "high", "explanation": "e"})
            )
        )
        assert (await validator.validate("q", "s", [], 0)).confidence == 50

    async def test_float_confidence_truncated(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(json.dumps({"confidence": 88.7}))
        )
        assert (await validator.validate("q", "s", [], 0)).confidence == 88

    async def test_invalid_json_returns_moderate_confidence(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response("this is not json {")
        )
        result = await validator.validate("q", "s", [], 0)
        assert result.confidence == 60
        assert result.is_acceptable is False
        assert "parsing failed" in result.explanation.lower()


class TestValidateErrors:
    """Error handling and mapping to LLM error types."""

    async def test_empty_choices_raises_llm_error(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(None, empty_choices=True)
        )
        with pytest.raises(LLMError, match="empty response"):
            await validator.validate("q", "s", [], 0)

    async def test_empty_content_raises_llm_error(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_response(None)
        )
        with pytest.raises(LLMError, match="empty message"):
            await validator.validate("q", "s", [], 0)

    async def test_timeout_mapped_to_llm_timeout(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=TimeoutError()
        )
        with pytest.raises(LLMTimeoutError, match="timed out"):
            await validator.validate("q", "s", [], 0)

    async def test_authentication_error_mapped(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=Exception("Error: authentication failed, invalid api_key")
        )
        with pytest.raises(LLMUnavailableError, match="authentication"):
            await validator.validate("q", "s", [], 0)

    async def test_rate_limit_error_mapped(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=Exception("rate_limit exceeded for requests")
        )
        with pytest.raises(LLMUnavailableError, match="rate limit"):
            await validator.validate("q", "s", [], 0)

    async def test_generic_error_mapped_to_llm_error(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection reset")
        )
        with pytest.raises(LLMError, match="connection reset"):
            await validator.validate("q", "s", [], 0)

    async def test_llm_error_passthrough(self) -> None:
        validator = make_validator()
        original = LLMError(message="original error")
        validator.client.chat.completions.create = AsyncMock(side_effect=original)
        with pytest.raises(LLMError) as exc_info:
            await validator.validate("q", "s", [], 0)
        assert exc_info.value is original


class TestPromptBuilding:
    """Tests for the validation prompt template."""

    def test_prompt_contains_all_parts(self) -> None:
        prompt = build_validation_prompt(
            question="How many users?",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 7}],
            row_count=7,
        )
        assert "How many users?" in prompt
        assert "SELECT COUNT(*) FROM users" in prompt
        assert '"count": 7' in prompt
        assert "showing 1 of 7 rows" in prompt

    def test_prompt_handles_unicode(self) -> None:
        prompt = build_validation_prompt(
            question="有多少患者?",
            sql="SELECT COUNT(*) FROM patients",
            results=[{"patient_count": 4823}],
            row_count=1,
        )
        assert "有多少患者?" in prompt
        assert "4823" in prompt
