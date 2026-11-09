"""Regression coverage for scenario validation, source consistency and cache failures."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pretextforge.bundle import generate
from pretextforge.cli import main
from pretextforge.conversations import validate_turn
from pretextforge.engine import arrival_key, digest, json_text, jsonl
from pretextforge.models import SOURCES, Baseline, Scenario, load_scenario, timestamp
from pretextforge.quality import evaluate
from pretextforge.rendering import ModelConfig, plans_for, render

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world():
    s = load_scenario(ROOT / "scenarios/acme.yaml")
    return Scenario.model_validate(
        {
            **s.model_dump(),
            "actions": s.actions[:2],
            "baseline": Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0),
        }
    )


class CopyRenderer:
    config = ModelConfig(model="review-test-fake", revision="1")

    def complete(self, system, payload, *, seed):
        return payload["turns"]


def refresh_hashes(bundle):
    manifest = json.loads((bundle / "MANIFEST.json").read_text())
    manifest["files"] = {name: digest(bundle / name) for name in manifest["files"]}
    (bundle / "MANIFEST.json").write_text(json_text(manifest))


def save_truth(bundle, truth):
    rows = sorted(truth["records"], key=arrival_key)
    truth["records"] = rows
    (bundle / "GROUND_TRUTH.json").write_text(json_text(truth))
    (bundle / "labels/cases.jsonl").write_text(jsonl(truth["cases"]))
    for source in SOURCES:
        (bundle / f"logs/{source}.jsonl").write_text(
            jsonl([r for r in rows if r["source"] == source])
        )
    refresh_hashes(bundle)


def test_complete_profile_cannot_silently_drop_events(world, tmp_path):
    bundle = tmp_path / "bundle"
    generate(world, bundle)
    observations = json.loads((bundle / "OBSERVATION_MANIFEST.json").read_text())
    for row in observations:
        if row["source"] == "telephony":
            row["dropped"] = True
    (bundle / "OBSERVATION_MANIFEST.json").write_text(json_text(observations))
    (bundle / "logs/telephony.jsonl").write_text("")
    refresh_hashes(bundle)
    assert not evaluate(bundle)["passed"]


def test_lifecycle_cannot_mix_user_accounts(world, tmp_path):
    bundle = tmp_path / "bundle"
    generate(world, bundle)
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    row = next(r for r in truth["records"] if r["source"] == "itsm")
    row["data"]["user"] = next(u.id for u in world.users if u.id != row["data"]["user"])
    save_truth(bundle, truth)
    assert not evaluate(bundle)["passed"]


def test_missing_approval_reports_failure_instead_of_crashing(world, tmp_path):
    bundle = tmp_path / "bundle"
    generate(world, bundle)
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    removed = {
        r["record_id"] for r in truth["records"] if r["data"].get("action") == "approval_granted"
    }
    truth["records"] = [r for r in truth["records"] if r["record_id"] not in removed]
    for case in truth["cases"]:
        case["record_ids"] = [rid for rid in case["record_ids"] if rid not in removed]
    observations = json.loads((bundle / "OBSERVATION_MANIFEST.json").read_text())
    (bundle / "OBSERVATION_MANIFEST.json").write_text(
        json_text([r for r in observations if r["record_id"] not in removed])
    )
    save_truth(bundle, truth)
    assert not evaluate(bundle)["passed"]


def test_published_plan_clocks_match_actual_utterances(world, tmp_path):
    cache, bundle = tmp_path / "cache", tmp_path / "bundle"
    assert render(plans_for(world), cache, CopyRenderer())["passed"]
    generate(world, bundle, render_cache=cache)
    path = bundle / "TURN_PLANS.json"
    plans = json.loads(path.read_text())
    plans[0]["turns"][0]["event_time"] = timestamp(world.start)
    path.write_text(json_text(plans))
    refresh_hashes(bundle)
    assert not evaluate(bundle)["passed"]


@pytest.mark.parametrize(
    "stage,text",
    [
        ("recovered", "I cannot sign in yet. Thank you."),
        ("vpn_fixed", "The VPN is not working. Thanks."),
        ("policy", "Manager approval is required. Your manager has approved this."),
    ],
)
def test_contradictory_renderings_are_rejected(world, stage, text):
    if stage == "vpn_fixed":
        world = world.model_copy(
            update={
                "actions": [
                    world.actions[0].model_copy(
                        update={"request": "vpn", "verification": "approved"}
                    )
                ]
            }
        )
    turn = next(t for p in plans_for(world) for t in p.turns if t.stage == stage)
    assert validate_turn(turn, text)


@pytest.mark.parametrize(
    "changes",
    [
        {"at": "2026-03-02T10:30:00"},
        {"id": "routine-0-0"},
        {"verification": "approved", "execute": False},
    ],
)
def test_invalid_or_unsupported_actions_fail_at_validation(world, changes):
    actions = [{**world.actions[0].model_dump(), **changes}, world.actions[1].model_dump()]
    with pytest.raises(ValidationError):
        Scenario.model_validate({**world.model_dump(), "actions": actions})


def test_phone_identity_comparison_ignores_presentation(world):
    phone = world.users[0].phone.replace("-", "")
    with pytest.raises(ValidationError):
        Scenario.model_validate({**world.model_dump(), "helpdesk_number": phone})


def test_renderer_honors_requested_styles(world, tmp_path):
    client = CopyRenderer()
    styles = []

    def complete(system, payload, *, seed):
        styles.append(payload["style"])
        return payload["turns"]

    client.complete = complete
    plans = plans_for(world)
    plans[0] = plans[0].model_copy(update={"style": "formal"})
    assert render(plans, tmp_path / "cache", client)["passed"]
    assert set(styles) == {"formal", "natural and concise"}


@pytest.mark.parametrize(
    "damage",
    [
        "actor",
        "file_count",
        "migration_approver",
        "speech_duration",
        "conversation_id",
        "call_id",
        "recovery_anchor",
    ],
)
def test_cross_source_corruption_cannot_pass_with_refreshed_hashes(tmp_path, damage):
    s = load_scenario(ROOT / "scenarios/acme.yaml").model_copy(
        update={
            "baseline": Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0)
        }
    )
    bundle = tmp_path / "bundle"
    generate(s, bundle)
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    if damage == "actor":
        next(r for r in truth["records"] if r["data"].get("event_type") == "mfa_reset")["data"][
            "actor"
        ] = s.actions[0].user
    elif damage == "file_count":
        removed = next(
            r for r in reversed(truth["records"]) if r["data"].get("action") == "download"
        )
        rid = removed["record_id"]
        truth["records"].remove(removed)
        for case in truth["cases"]:
            case["record_ids"] = [i for i in case["record_ids"] if i != rid]
        path = bundle / "OBSERVATION_MANIFEST.json"
        path.write_text(
            json_text([r for r in json.loads(path.read_text()) if r["record_id"] != rid])
        )
    elif damage == "migration_approver":
        next(
            r
            for r in truth["records"]
            if r["data"].get("category") == "migration"
            and r["data"].get("action") == "approval_granted"
        )["data"]["approver"] = s.users[0].id
    elif damage == "speech_duration":
        next(r for r in truth["records"] if r["source"] == "transcripts")["data"]["duration_s"] = (
            1000
        )
    elif damage == "conversation_id":
        next(r for r in truth["records"] if r["source"] == "transcripts")["data"][
            "conversation_id"
        ] = "unrelated"
    elif damage == "call_id":
        for row in truth["records"]:
            if row["source"] == "telephony":
                row["data"]["call_id"] = "unrelated"
    else:
        case = next(c for c in truth["cases"] if c["kind"] == "helpdesk" and c["action_point"])
        case["recovery_confirmation_record_id"] = next(
            r["record_id"]
            for r in truth["records"]
            if r["correlation_id"] == case["correlation_id"] and r["source"] == "transcripts"
        )
    save_truth(bundle, truth)
    assert not evaluate(bundle)["passed"]


def test_source_outcomes_must_match_scenario_even_after_rehash(world, tmp_path):
    bundle = tmp_path / "bundle"
    generate(world, bundle)
    path = bundle / "resolved_scenario.json"
    scenario = json.loads(path.read_text())
    scenario["actions"][0]["execute"] = False
    path.write_text(json_text(scenario))
    refresh_hashes(bundle)
    assert not evaluate(bundle)["passed"]


def test_yaml_sequence_mapping_key_is_a_clean_validation_failure(tmp_path, capsys):
    path = tmp_path / "invalid.yaml"
    path.write_text("? [not, a, scalar]\n: value\n")
    assert main(["validate", str(path)]) == 1
    assert "mapping keys must be scalar" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["name", "helpdesk_agent"])
def test_required_identity_metadata_cannot_be_blank(world, field):
    with pytest.raises(ValidationError):
        Scenario.model_validate({**world.model_dump(), field: " "})


def test_employee_name_and_action_id_cannot_be_whitespace(world):
    data = world.model_dump()
    data["users"][0]["name"] = " "
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)
    data = world.model_dump()
    data["actions"][0]["id"] = " "
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)
