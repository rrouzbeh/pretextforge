"""Offline authoring, cache integrity and transcript-validation tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pretextforge.bundle import generate
from pretextforge.conversations import content_hash, validate_turn
from pretextforge.engine import json_text
from pretextforge.models import Baseline, load_scenario
from pretextforge.quality import evaluate
from pretextforge.rendering import ModelConfig, plans_for, read_cache, render

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def small():
    scenario = load_scenario(ROOT / "scenarios/acme.yaml")
    return scenario.model_copy(
        update={
            "actions": scenario.actions[:2],
            "baseline": Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0),
        }
    )


class FakeRenderer:
    config = ModelConfig(model="unit-test-fake", revision="fixture-1")

    def __init__(self):
        self.requests = []

    def complete(self, system, payload, *, seed):
        self.requests.append(payload)
        return {field: turn for field, turn in payload["turns"].items()}


def test_private_plans_do_not_change_template_evidence(small, tmp_path):
    template, cached = tmp_path / "template", tmp_path / "cached"
    client = FakeRenderer()
    plans = plans_for(small)
    cache = tmp_path / "cache"
    assert render(plans, cache, client)["passed"]
    generate(small, template)
    generate(small, cached, render_cache=cache)
    assert evaluate(cached)["passed"]
    for file in (template / "logs").glob("*"):
        assert file.read_bytes() == (cached / "logs" / file.name).read_bytes()
    assert (template / "labels/cases.jsonl").read_bytes() == (
        cached / "labels/cases.jsonl"
    ).read_bytes()
    assert not (template / "TURN_PLANS.json").exists()
    assert (cached / "TURN_PLANS.json").exists()
    first = client.requests[0]
    assert "turns" in first
    assert "malicious" not in json.dumps(client.requests)
    assert "true_actor" not in json.dumps(client.requests)


def test_missing_stale_and_corrupt_cache_fail_atomically(small, tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="cache"):
        generate(small, tmp_path / "missing", render_cache=cache)
    assert not (tmp_path / "missing").exists()
    plans = plans_for(small)
    render(plans, cache, FakeRenderer())
    stale = plans[0].model_copy(update={"style": "different"})
    with pytest.raises(ValueError, match="cache"):
        read_cache([stale], cache)
    file = next((cache / "objects").glob("*.json"))
    entry = json.loads(file.read_text())
    entry["texts"][0] = "An invented speaker has approved this."
    file.write_text(json_text(entry))
    with pytest.raises(ValueError, match="cache"):
        generate(small, tmp_path / "corrupt", render_cache=cache)
    assert not (tmp_path / "corrupt").exists()


def test_render_resume_makes_no_model_calls(small, tmp_path):
    plans, client = plans_for(small), FakeRenderer()
    cache = tmp_path / "cache"
    render(plans, cache, client)
    client.complete = lambda *args, **kwargs: pytest.fail("model called for accepted cached text")
    assert render(plans, cache, client)["passed"]
    generate(small, tmp_path / "offline", render_cache=cache)


@pytest.mark.parametrize(
    "wrong_text",
    [
        "I need my manager to approve this.",
        "I will request our supervisor's approval now.",
    ],
)
def test_renderer_preserves_whose_manager_approves(small, tmp_path, wrong_text):
    plans = plans_for(small)
    plan = next(p for p in plans if any(t.stage == "request_approval" for t in p.turns))
    turn = next(t for t in plan.turns if t.stage == "request_approval")
    assert "agent refers to own manager instead of caller manager" in validate_turn(
        turn, wrong_text
    )
    assert not validate_turn(turn, turn.template_text)
    cache, client = tmp_path / "cache", FakeRenderer()
    assert render([plan], cache, client)["passed"]
    # Even a correctly rehashed old cache cannot bypass current semantic validation.
    key = json.loads((cache / "index.json").read_text())["entries"][plan.conversation_id]
    file = cache / "objects" / f"{key}.json"
    entry = json.loads(file.read_text())
    entry["texts"][turn.index - 1] = wrong_text
    entry["payload_sha256"] = content_hash({k: entry[k] for k in ("plan", "config", "texts")})
    file.write_text(json_text(entry))
    with pytest.raises(ValueError, match="caller manager"):
        read_cache([plan], cache)


def test_retries_do_not_accept_bad_text(small, tmp_path):
    client = FakeRenderer()
    client.complete = lambda *args, **kwargs: {"text": "I will reset MFA now without approval."}
    result = render(plans_for(small), tmp_path / "cache", client, attempts=2)
    assert not result["passed"]
    assert not list((tmp_path / "cache/objects").glob("*.json"))
    assert list((tmp_path / "cache/failures").glob("*.json"))
    with pytest.raises(ValueError, match="attempts"):
        render(plans_for(small), tmp_path / "invalid", client, attempts=0)


def test_last_copy_retry_is_explicitly_recorded(small, tmp_path):
    client = FakeRenderer()

    def complete(system, payload, *, seed):
        return {
            field: shape.get("enum", [""])[0]
            for field, shape in client.response_schema["properties"].items()
        }

    client.complete = complete
    cache = tmp_path / "cache"
    result = render(plans_for(small), cache, client)
    assert result["passed"]
    entries = [json.loads(p.read_text()) for p in (cache / "objects").glob("*.json")]
    assert all(
        any(a["mode"] == "constrained_source_repair" and not a["errors"] for a in entry["attempts"])
        for entry in entries
    )


def test_slot_replacement_handles_json_characters(tmp_path):
    from pretextforge.rendering import shared_completion

    client = FakeRenderer()
    client.complete = lambda system, payload, seed: {"text": payload["utterance"]}
    text = 'This is Dana "Ortiz". Please help.'
    result, _ = shared_completion(
        client,
        "test",
        {"utterance": text},
        tmp_path,
        retry=0,
        substitutions={'Dana "Ortiz"': "SLOT_0"},
    )
    assert result["text"] == text


def test_required_slots_and_sensitive_polarity(small):
    plans = plans_for(small)
    for plan in plans:
        for turn in plan.turns:
            assert validate_turn(turn, turn.template_text) == []
    name_turn = plans[0].turns[0]
    assert validate_turn(name_turn, "Hi, this is somebody else. I need an MFA reset.")
    hold = plans[0].turns[-1].model_copy(update={"acts": ["action_hold"]})
    assert validate_turn(hold, "I will reset MFA now.")
    approved = next(t for t in plans[1].turns if "approval_claim" in t.acts)
    assert validate_turn(approved, "Manager approval is not recorded.")
    assert validate_turn(approved, "Manager approval is recorded. I will now request it.")


def test_cached_text_fresh_process_byte_determinism(small, tmp_path):
    import yaml

    cache = tmp_path / "cache"
    render(plans_for(small), cache, FakeRenderer())
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump(small.model_dump(mode="json")))
    outputs = [tmp_path / "first", tmp_path / "second"]
    for seed, output in zip(("1", "999"), outputs, strict=True):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pretextforge.cli",
                "generate",
                str(scenario),
                "--text-mode",
                "cached_llm",
                "--render-cache",
                str(cache),
                "--output",
                str(output),
            ],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
        )
    from pretextforge.engine import digest

    hashes = [
        {p.relative_to(output).as_posix(): digest(p) for p in output.rglob("*") if p.is_file()}
        for output in outputs
    ]
    assert hashes[0] == hashes[1]


def test_cached_mode_checks_provenance_even_with_updated_hashes(small, tmp_path):
    cache, bundle = tmp_path / "cache", tmp_path / "bundle"
    render(plans_for(small), cache, FakeRenderer())
    generate(small, bundle, render_cache=cache)
    manifest = json.loads((bundle / "MANIFEST.json").read_text())
    cid = next(iter(manifest["rendering"]["entries"]))
    manifest["rendering"]["entries"][cid] = content_hash("tampered")
    (bundle / "MANIFEST.json").write_text(json_text(manifest))
    result = evaluate(bundle)
    assert not result["passed"]
    assert "rendering provenance mismatch" in result["errors"]


@pytest.mark.packaged
def test_packaged_real_model_example_generates_without_network(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        pytest.fail("offline generation attempted a model/network call")

    monkeypatch.setattr("socket.create_connection", unavailable)
    monkeypatch.setattr("pretextforge.rendering.Endpoint.complete", unavailable)
    scenario = load_scenario(ROOT / "scenarios/llm-pilot.yaml")
    output = tmp_path / "real-cached-example"
    report = generate(scenario, output, render_cache=ROOT / "examples/llm-pilot-cache")
    assert report["passed"]
    manifest = json.loads((output / "MANIFEST.json").read_text())
    assert manifest["rendering"]["config"]["model"] == "qwen2.5-1.5b-instruct-q4_k_m"
    assert manifest["rendering"]["quality"]["changed_turns"] > 0
    assert evaluate(output)["passed"]
