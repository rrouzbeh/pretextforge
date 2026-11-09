"""Portable accepted text remains usable without an inference endpoint."""

import json
from pathlib import Path

import pytest

from pretextforge.cli import main
from pretextforge.quality import evaluate
from pretextforge.rendering import Endpoint

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.packaged
def test_audit_and_export_support_offline_generation(tmp_path, monkeypatch):
    def no_inference(*args, **kwargs):
        pytest.fail("offline audit/export/generation attempted inference")

    monkeypatch.setattr(Endpoint, "complete", no_inference)
    scenario = str(ROOT / "scenarios/llm-pilot.yaml")
    cache = str(ROOT / "examples/llm-pilot-cache")
    audit = tmp_path / "audit"
    exported = tmp_path / "exported"
    bundle = tmp_path / "bundle"
    assert main(["audit", scenario, "--cache", cache, "--output", str(audit)]) == 0
    report = json.loads((audit / "report.json").read_text())
    assert report["candidate_attempts"] > 0
    assert "Transcript review packet" in (audit / "transcripts.md").read_text()
    assert main(["cache-export", scenario, "--cache", cache, "--output", str(exported)]) == 0
    assert not (exported / "requests").exists()
    assert (
        main(
            [
                "generate",
                scenario,
                "--text-mode",
                "cached_llm",
                "--render-cache",
                str(exported),
                "--output",
                str(bundle),
            ]
        )
        == 0
    )
    assert evaluate(bundle)["passed"]
    assert not (bundle / "traces.jsonl").exists()
    for command, output in (("audit", audit), ("cache-export", exported)):
        before = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
        assert main([command, scenario, "--cache", cache, "--output", str(output)]) == 1
        assert before == {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}


@pytest.mark.parametrize("command", ["replay", "extract", "claims-export", "freeze", "compare"])
def test_removed_detection_commands_are_unavailable(command):
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2
