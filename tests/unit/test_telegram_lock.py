"""Tests for the cross-process getUpdates lock (issue #24)."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning.telegram_lock import getupdates_lock, lock_path


# lock_path() now derives from PROJECT_ROOT / "data/..." (no SQLite DB path).
# To keep these tests hermetic and isolated from the real lock file, we point
# the lock helper at a per-test tmp dir.
@pytest.fixture()
def tmp_lock(tmp_path):
    lock_file = tmp_path / "telegram_getupdates.lock"
    with patch("aibp.self_learning.telegram_lock.lock_path", return_value=lock_file):
        yield tmp_path


def test_lock_path_lives_in_data_dir():
    """lock_path() is relative to the project data directory (no DB path)."""
    from aibp.utils.config import PROJECT_ROOT

    assert lock_path() == PROJECT_ROOT / "data" / "telegram_getupdates.lock"


def test_single_holder_acquires(tmp_lock):
    with getupdates_lock() as acquired:
        assert acquired is True


def test_second_acquire_is_refused_while_held(tmp_lock):
    """A second concurrent holder must be refused (non-blocking) — this is the
    exact 409-race the lock prevents."""
    with getupdates_lock() as first:
        assert first is True
        with getupdates_lock() as second:
            assert second is False


def test_lock_is_released_after_exit(tmp_lock):
    with getupdates_lock() as first:
        assert first is True
    # Once released, it can be acquired again
    with getupdates_lock() as again:
        assert again is True


def test_lock_released_even_on_exception(tmp_lock):
    with pytest.raises(ValueError):  # noqa: PT012
        with getupdates_lock() as acquired:
            assert acquired is True
            raise ValueError("boom")
    with getupdates_lock() as again:
        assert again is True
