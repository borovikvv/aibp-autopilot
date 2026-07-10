# tests/unit/test_simulate_thresholds.py
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.simulate_thresholds as sim


def test_simulate_no_experiments(capsys):
    with patch("scripts.simulate_thresholds.fetch_all", return_value=[]):
        result = sim.run()
    out = capsys.readouterr().out
    assert result == 0
    assert "insufficient history" in out or "No finished" in out
