"""Pytest configuration — unit tests stay hermetic (no live PG).

Integration tests (tests/integration/) require TEST_DATABASE_URL and are
skipped automatically when it is not set.
"""
import os

import pytest

# Integration tests need a real PostgreSQL; skip without the env var so plain
# `pytest` never fails on a dev machine without PG.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set — integration tests need a live PostgreSQL",
)
