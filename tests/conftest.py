"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.
"""

import os

import pytest

from pg_mcp.config.settings import reset_settings
from pg_mcp.observability.metrics import metrics


@pytest.fixture(autouse=True)
def reset_config(tmp_path, monkeypatch) -> None:
    """Reset global settings and isolate tests from any project .env file.

    Nested ``*Config`` classes read ``.env`` from the working directory;
    chdir into a temp dir so test expectations are independent of whatever
    configuration happens to sit next to the source tree.
    """
    monkeypatch.chdir(tmp_path)
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable metrics recording and HTTP server for tests."""
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    metrics.enabled = False
    yield
    metrics.enabled = True
    # Clean up
    if "OBSERVABILITY_METRICS_ENABLED" in os.environ:
        del os.environ["OBSERVABILITY_METRICS_ENABLED"]
