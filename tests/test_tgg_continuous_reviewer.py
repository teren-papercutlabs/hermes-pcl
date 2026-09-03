from __future__ import annotations

import hashlib
import importlib.util
import json
import sys


def _load():
    path = __import__("pathlib").Path(__file__).parents[1] / "scripts" / "tgg_continuous_reviewer.py"
    spec = importlib.util.spec_from_file_location("pa97_adapter", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_adapter_preserves_child_result_contract_with_ephemeral_carrier(monkeypatch, tmp_path):
    subject = _load(); batch = tmp_path / "batch"; batch.mkdir()
    candidate = {"source_group_inventory": ["g1"]}
    candidate_path = batch / "continuous-candidate-final.json"
    raw = json.dumps(candidate, sort_keys=True, separators=(",", ":")) .encode() + b"\n"
    candidate_path.write_bytes(raw)
    launch = {"contract": subject.CONTRACT, "batch_id": "nightly:test", "candidate_path": str(candidate_path),
              "candidate_sha256": hashlib.sha256(raw).hexdigest(), "review_authority": "a" * 64,
              "allowed_tools": ["tgg_nightly_review_get_candidate", "tgg_nightly_review_submit"], "max_iterations": 24,
              "reviewer_provider": "openai-codex"}
    launch_path = batch / "launch.json"; launch_path.write_text(json.dumps(launch))
    def fake_run(**kwargs):
        assert kwargs["allowed_tool_names"] == launch["allowed_tools"]
        assert kwargs["max_iterations"] == 24
        assert kwargs["provider"] == "openai-codex"
        assert json.dumps(candidate) not in kwargs["prompt"]
        assert str(candidate_path) not in kwargs["prompt"]
        assert launch["review_authority"] in kwargs["prompt"]
        assert launch["review_authority"] not in kwargs["system_prompt"]
        (batch / "continuous-reviewed-final.json").write_text("{}")
        return {"final_response": "ok"}, {"session_id": "pa97-review-clean", "loaded_tools": launch["allowed_tools"], "cleanup": {"deleted": True}}
    monkeypatch.setattr("hermes_cli.ephemeral_session.run_ephemeral_session", fake_run)
    monkeypatch.setattr(sys, "argv", ["reviewer", "--launch", str(launch_path), "--result", str(batch / "result.json")])
    assert subject.main() == 0
    result = json.loads((batch / "result.json").read_text())
    assert result["contract"] == subject.RESULT_CONTRACT
    assert result["status"] == "completed"
    assert result["allowed_tools"] == launch["allowed_tools"]
    assert result["lifecycle"]["cleanup"]["deleted"] is True
    assert result["reviewer_provider"] == "openai-codex"
    assert "a" * 64 not in json.dumps(result)


def test_launch_refuses_missing_or_non_codex_provider():
    subject = _load()
    base = {"contract": subject.CONTRACT, "batch_id": "b", "candidate_sha256": "a", "candidate_path": "/tmp/c",
            "allowed_tools": ["tgg_nightly_review_submit"]}
    import pytest
    with pytest.raises(RuntimeError, match="LAUNCH_INVALID"):
        subject.validate_launch(base)
    with pytest.raises(RuntimeError, match="LAUNCH_INVALID"):
        subject.validate_launch({**base, "reviewer_provider": "openrouter"})
