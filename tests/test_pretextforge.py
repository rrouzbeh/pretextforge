"""Invariant, corruption and end-to-end tests for dataset contracts."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from pretextforge.bundle import generate
from pretextforge.cli import main
from pretextforge.engine import Builder, arrival_key, digest, json_text, observe
from pretextforge.models import Baseline, Scenario, load_scenario
from pretextforge.quality import evaluate, read_jsonl

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario():
    return load_scenario(ROOT / "scenarios/acme.yaml")


@pytest.fixture
def bundle(tmp_path, scenario):
    path = tmp_path / "bundle"
    generate(scenario, path)
    return path


def mutate(scenario, **changes):
    return Scenario.model_validate({**scenario.model_dump(), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown_field": True},
        {"workdays": 6},
        {"start": "2026-03-02T09:00:00"},
        {"baseline": {"daily_helpdesk": -1}},
    ],
)
def test_invalid_scenario(scenario, changes):
    with pytest.raises(ValidationError):
        mutate(scenario, **changes)


@pytest.mark.parametrize(
    "change",
    [
        {"user": "missing"},
        {"at": "2030-01-01T00:00:00Z"},
        {"verification": "approved", "user": "jordan.kim"},
        {"at": "2026-03-07T08:59:00Z"},
    ],
)
def test_bad_action_references_and_timing(scenario, change):
    action = {**scenario.actions[0].model_dump(), **change}
    with pytest.raises(ValidationError):
        mutate(scenario, actions=[action])


def test_duplicates_and_overlap(scenario):
    with pytest.raises(ValidationError, match="unique"):
        mutate(scenario, users=[scenario.users[0], scenario.users[0]])
    action = scenario.actions[0]
    with pytest.raises(ValidationError, match="overlapping"):
        mutate(scenario, actions=[action, action.model_copy(update={"id": "overlap"})])


def test_complete_bundle_quality(bundle):
    quality = evaluate(bundle)
    assert quality["passed"], quality["errors"]
    assert quality["dropped_records"] == 0
    assert quality["attacks"] == 2
    assert len(quality["records_per_source"]) == 6
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    observed = [r for path in (bundle / "logs").glob("*.jsonl") for r in read_jsonl(path)]
    assert {r["record_id"] for r in observed} == {r["record_id"] for r in truth["records"]}
    forbidden = {"malicious", "true_actor", "class", "case_id", "acts"}
    assert all(not forbidden.intersection(r["data"]) for r in observed)
    assert not (bundle / "traces.jsonl").exists()


def test_degraded_source_local_loss_and_arrival_order(scenario, tmp_path):
    degraded = mutate(scenario, observation="degraded")
    path = tmp_path / "degraded"
    generate(degraded, path)
    assert evaluate(path)["passed"]
    records, _ = Builder(degraded).build()
    visible, decisions = observe(records, degraded)
    assert visible == sorted(visible, key=arrival_key)
    dropped = [d for d in decisions if d["dropped"]]
    assert dropped
    assert all(d["source"] in {"telephony", "transcripts"} for d in dropped)
    # Missing transcripts must not delete a ticket from the same lifecycle.
    missing_transcript = next(d for d in dropped if d["source"] == "transcripts")
    assert any(
        r["correlation_id"] == missing_transcript["correlation_id"] and r["source"] == "itsm"
        for r in visible
    )
    # A real approval may arrive after the reset even though it happened first.
    approved = next(
        r
        for r in visible
        if r["source"] == "itsm"
        and r["data"]["category"] == "mfa_reset"
        and r["data"]["action"] == "approval_granted"
    )
    reset = next(
        r
        for r in visible
        if r["source"] == "idp"
        and r["correlation_id"] == approved["correlation_id"]
        and r["data"]["event_type"] == "mfa_reset"
    )
    assert approved["event_time"] < reset["event_time"] < approved["available_at"]


def test_intent_is_not_procedural_evidence(scenario):
    one = mutate(
        scenario,
        actions=[scenario.actions[0]],
        baseline=Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0),
    )
    two = mutate(one, actions=[one.actions[0].model_copy(update={"malicious": False})])
    records_a, cases_a = Builder(one).build()
    records_b, cases_b = Builder(two).build()
    assert records_a == records_b
    assert cases_a[0]["malicious"] != cases_b[0]["malicious"]
    assert cases_a[0]["procedure_satisfied"] is False
    assert cases_b[0]["procedure_satisfied"] is False


def test_factor_state_and_no_early_duration(bundle):
    records = json.loads((bundle / "GROUND_TRUTH.json").read_text())["records"]
    events = [r for r in records if r["source"] == "idp"]
    enrollments = [r for r in events if r["data"]["event_type"] == "factor_enroll"]
    removals = [r for r in events if r["data"]["event_type"] == "factor_remove"]
    assert removals[1]["data"]["factor_id"] == enrollments[0]["data"]["factor_id"]
    for row in records:
        if row["source"] == "telephony" and row["data"]["event_type"] == "started":
            assert row["data"]["duration_s"] is None


@pytest.mark.parametrize("damage", ["schema", "missing", "duplicate", "labels", "extra", "causal"])
def test_corruption_is_detected(bundle, damage):
    if damage == "schema":
        path = bundle / "logs/idp.jsonl"
        rows = read_jsonl(path)
        rows[0]["data"]["unexpected"] = "bad"
        path.write_text(json.dumps(rows[0]) + "\n")
    elif damage == "missing":
        (bundle / "logs/itsm.jsonl").unlink()
    elif damage == "duplicate":
        path = bundle / "logs/hr.jsonl"
        path.write_text(path.read_text() * 2)
    elif damage == "labels":
        (bundle / "labels/cases.jsonl").write_text("")
    elif damage == "extra":
        (bundle / "logs/secret.json").write_text("{}")
    else:
        path = bundle / "GROUND_TRUTH.json"
        truth = json.loads(path.read_text())
        truth["records"] = [
            r for r in truth["records"] if r["data"].get("event_type") != "factor_enroll"
        ]
        path.write_text(json_text(truth))
        # Even when file hashes are refreshed, semantic checks must detect damage.
        manifest = json.loads((bundle / "MANIFEST.json").read_text())
        manifest["files"]["GROUND_TRUTH.json"] = digest(path)
        (bundle / "MANIFEST.json").write_text(json_text(manifest))
    assert evaluate(bundle)["passed"] is False


def test_baseline_only_and_empty_world_activity(scenario, tmp_path):
    baseline = mutate(scenario, actions=[])
    generate(baseline, tmp_path / "baseline")
    assert "No attack was generated" in (tmp_path / "baseline/GROUND_TRUTH.md").read_text()
    empty = mutate(
        baseline,
        baseline=Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0),
    )
    assert generate(empty, tmp_path / "empty")["canonical_records"] == 0
    assert evaluate(tmp_path / "empty")["attacks"] == 0


def test_denied_reset_does_not_create_factor_events(scenario, tmp_path):
    action = scenario.actions[0].model_copy(update={"execute": False, "channel": "chat"})
    quiet = Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0)
    scenario = mutate(scenario, actions=[action], baseline=quiet)
    path = tmp_path / "denied"
    generate(scenario, path)
    assert read_jsonl(path / "logs/idp.jsonl") == []
    assert read_jsonl(path / "logs/telephony.jsonl") == []


def test_output_protection_and_atomic_failure(scenario, tmp_path, monkeypatch):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "important.txt").write_text("keep")
    with pytest.raises(ValueError, match="absent or empty"):
        generate(scenario, output)
    assert (output / "important.txt").read_text() == "keep"
    monkeypatch.setattr(
        "pretextforge.bundle.evaluate", lambda _: {"passed": False, "errors": ["test"]}
    )
    with pytest.raises(ValueError, match="failed quality"):
        generate(scenario, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob(".pforge-*"))


def test_cli(bundle, tmp_path, capsys):
    assert main(["validate", str(ROOT / "scenarios/acme.yaml")]) == 0
    assert main(["eval", str(bundle)]) == 0
    assert (
        main(
            [
                "generate",
                str(ROOT / "scenarios/acme.yaml"),
                "--baseline-only",
                "--output",
                str(tmp_path / "cli-baseline"),
            ]
        )
        == 0
    )
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("name: broken\n")
    assert main(["validate", str(invalid)]) == 1
    assert "pforge:" in capsys.readouterr().err


def test_fresh_process_determinism(tmp_path):
    outputs = [tmp_path / "a", tmp_path / "b"]
    for seed, output in zip(["1", "999"], outputs, strict=True):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pretextforge.cli",
                "generate",
                str(ROOT / "scenarios/acme.yaml"),
                "--output",
                str(output),
            ],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
    hashes = [
        {p.relative_to(output).as_posix(): digest(p) for p in output.rglob("*") if p.is_file()}
        for output in outputs
    ]
    assert hashes[0] == hashes[1]


def test_seed_changes_data(scenario):
    other = mutate(scenario, seed=7)
    assert Builder(scenario).build()[0] != Builder(other).build()[0]


def test_naive_record_time_rejected():
    from pretextforge.models import Record

    with pytest.raises(ValidationError):
        Record(
            record_id="x",
            source="idp",
            event_time="2026-03-02T10:00:00",
            available_at="2026-03-02T11:00:00Z",
            correlation_id="x",
            data={"event_type": "login", "actor": "a", "user": "a"},
        )


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicates.yaml"
    path.write_text("name: first\nname: second\n")
    with pytest.raises(ValueError, match="duplicate YAML"):
        load_scenario(path)


def test_arrival_order_handles_fractional_seconds():
    early = {
        "available_at": "2026-03-02T10:00:00Z",
        "event_time": "2026-03-02T10:00:00Z",
        "record_id": "a",
    }
    late = {**early, "available_at": "2026-03-02T10:00:00.500000Z", "record_id": "b"}
    assert sorted([late, early], key=arrival_key) == [early, late]


def test_record_cannot_arrive_before_it_happens():
    from pretextforge.models import Record

    with pytest.raises(ValidationError, match="precedes"):
        Record(
            record_id="x",
            source="idp",
            event_time="2026-03-02T10:00:00Z",
            available_at="2026-03-02T09:00:00Z",
            correlation_id="x",
            data={"event_type": "login", "actor": "a", "user": "a"},
        )


def test_refreshing_hashes_does_not_hide_wrong_labels(bundle):
    path = bundle / "GROUND_TRUTH.json"
    truth = json.loads(path.read_text())
    truth["cases"][0]["malicious"] = not truth["cases"][0]["malicious"]
    path.write_text(json_text(truth))
    from pretextforge.engine import jsonl

    labels = bundle / "labels/cases.jsonl"
    labels.write_text(jsonl(truth["cases"]))
    manifest_path = bundle / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["GROUND_TRUTH.json"] = digest(path)
    manifest["files"]["labels/cases.jsonl"] = digest(labels)
    manifest_path.write_text(json_text(manifest))
    result = evaluate(bundle)
    assert result["passed"] is False
    assert any("scenario/label mismatch" in error for error in result["errors"])
