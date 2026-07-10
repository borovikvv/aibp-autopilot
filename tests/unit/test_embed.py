# tests/unit/test_embed.py
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.enrichment.llm_client import OpenRouterClient  # noqa: E402


def test_embed_shapes_request():
    client = OpenRouterClient.__new__(OpenRouterClient)
    client.api_key = "test-key"
    client.default_model = "test-model"
    client.daily_budget = 100
    client._cost_log_dir = Path("/tmp")
    client._today_str = "20260708"
    client.cost_log = Path("/tmp/llm_cost_20260708.jsonl")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = client.embed(["test text"])
    assert result == [[0.1, 0.2, 0.3]]
    # Verify the request used the embeddings endpoint
    call_args = mock_client.post.call_args
    assert "embeddings" in call_args.args[0]
