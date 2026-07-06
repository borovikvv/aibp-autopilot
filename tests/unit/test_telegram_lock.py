"""Tests for the cross-process getUpdates lock (issue #24)."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import telegram_lock
from aibp.self_learning.telegram_lock import getupdates_lock, lock_path


@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(telegram_lock, "get_db_path", return_value=tmp_path / "test.db"):
        yield tmp_path


def test_lock_path_next_to_db(temp_db):
    assert lock_path().parent == temp_db


def test_single_holder_acquires(temp_db):
    with getupdates_lock() as acquired:
        assert acquired is True


def test_second_acquire_is_refused_while_held(temp_db):
    """A second concurrent holder must be refused (non-blocking) — this is the
    exact 409-race the lock prevents."""
    with getupdates_lock() as first:
        assert first is True
        with getupdates_lock() as second:
            assert second is False


def test_lock_is_released_after_exit(temp_db):
    with getupdates_lock() as first:
        assert first is True
    # Once released, it can be acquired again
    with getupdates_lock() as again:
        assert again is True


def test_lock_released_even_on_exception(temp_db):
    with pytest.raises(ValueError):  # noqa: PT012
        with getupdates_lock() as acquired:
            assert acquired is True
            raise ValueError("boom")
    with getupdates_lock() as again:
        assert again is True
