"""Cross-source timing, identity and conversation invariants."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pretextforge.bundle import generate
from pretextforge.conversations import validate_turn
from pretextforge.engine import Builder
from pretextforge.models import Baseline, Call, Scenario, load_scenario
from pretextforge.rendering import ModelConfig, plans_for, render

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world():
    s = load_scenario(ROOT / "scenarios/acme.yaml")
    return s.model_copy(
        update={
            "baseline": Baseline(daily_logins_per_user=0, daily_files_per_user=0, daily_helpdesk=0),
            "actions": s.actions[:2],
        }
    )


def test_service_phone_cannot_be_employee(world):
    with pytest.raises(ValidationError, match="collide"):
        Scenario.model_validate({**world.model_dump(), "helpdesk_number": world.users[0].phone})


@pytest.mark.parametrize("pretext", ["lost_phone", "replacement_device"])
def test_all_authored_phrase_variants_have_their_planned_meaning(world, pretext):
    world = world.model_copy(
        update={"actions": [a.model_copy(update={"pretext": pretext}) for a in world.actions]}
    )
    builder = Builder(world)
    builder.build()
    for plan in builder.plans:
        for turn in plan.turns:
            for phrase in builder.templates[turn.stage]:
                text = phrase.format(
                    name=next(u.name for u in world.users if u.id == plan.user),
                    number=world.actions[0].new_number,
                )
                literals = [v for v in turn.required_literals if not v.startswith("+")]
                if "{number}" in phrase:
                    literals.append(world.actions[0].new_number)
                assert not validate_turn(
                    turn.model_copy(update={"required_literals": literals, "template_text": text}),
                    text,
                )


def test_approval_and_recovery_words_follow_real_events(world):
    builder = Builder(world)
    records, cases = builder.build()
    lookup = {r["record_id"]: r for r in records}
    approved = next(c for c in cases if c["procedure_satisfied"])
    rows = [r for r in records if r["correlation_id"] == approved["correlation_id"]]
    grant = next(r for r in rows if r["data"].get("action") == "approval_granted")
    confirm = lookup[approved["approval_confirmation_record_id"]]
    speech_start = datetime.fromisoformat(confirm["event_time"]) - timedelta(
        seconds=confirm["data"]["duration_s"]
    )
    assert datetime.fromisoformat(grant["event_time"]) < speech_start
    login = next(r for r in rows if r["data"].get("event_type") == "login")
    recovery = next(
        t
        for p in builder.plans
        if p.conversation_id == approved["correlation_id"]
        for t in p.turns
        if t.stage == "recovered"
    )
    row = lookup[recovery.record_id]
    assert datetime.fromisoformat(login["event_time"]) < datetime.fromisoformat(
        row["event_time"]
    ) - timedelta(seconds=row["data"]["duration_s"])
    assert confirm["data"]["wait_reason"] == "manager_approval"


def test_rendered_words_drive_event_clocks(world, tmp_path):
    class LongerRenderer:
        config = ModelConfig(model="explicit-test-fake", revision="1")

        def complete(self, system, payload, *, seed):
            return {
                field: turn + " Please take the time you need."
                for field, turn in payload["turns"].items()
            }

    client = LongerRenderer()
    cache = tmp_path / "cache"
    assert render(plans_for(world), cache, client)["passed"]
    template = Builder(world)
    before, _ = template.build()
    from pretextforge.rendering import read_cache

    words, _ = read_cache(template.plans, cache)
    after, _ = Builder(world, transcripts=words).build()
    old = {r["record_id"]: r for r in before}
    turns = [r for r in after if r["source"] == "transcripts"]
    assert all(r["data"]["duration_s"] > old[r["record_id"]]["data"]["duration_s"] for r in turns)
    assert after[-1]["event_time"] > before[-1]["event_time"]
    assert generate(world, tmp_path / "bundle", render_cache=cache)["passed"]


def test_background_sessions_files_and_factor_state():
    s = load_scenario(ROOT / "scenarios/acme.yaml")
    rows, _ = Builder(s).build()
    sessions = {}
    factors = {}
    counts = Counter()
    object_sizes = defaultdict(set)
    for r in rows:
        d = r["data"]
        if r["source"] == "idp":
            if d["event_type"] == "factor_enroll":
                factors[d["user"]] = d["factor_id"]
            if d["event_type"] == "login":
                assert d["factor_id"] and d["device_id"] and d["src_ip"]
                if d["user"] in factors:
                    assert d["factor_id"] == factors[d["user"]]
                sessions[d["session_id"]] = r
                counts[r["event_time"][:10]] += 1
        if r["source"] == "saas_files":
            auth = sessions[d["session_id"]]
            assert auth["event_time"] < r["event_time"]
            assert all(auth["data"][k] == d[k] for k in ("device_id", "src_ip", "user"))
            object_sizes[(d["user"], d["object_id"])].add(d["bytes"])
    assert len(set(counts.values())) > 1
    assert all(len(sizes) == 1 for sizes in object_sizes.values())
    downloads = [
        r for r in rows if r["source"] == "saas_files" and r["data"]["action"] == "download"
    ]
    assert len(object_sizes) < len(downloads)


def test_enterprise_schedule_shares_time_bins_across_intent():
    s = load_scenario(ROOT / "scenarios/enterprise-week.yaml")
    bins = defaultdict(set)
    for action in s.actions:
        if isinstance(action, Call):
            bins[action.at.minute // 10].add(action.malicious)
    assert bins and all(values == {False, True} for values in bins.values())
    holdout = load_scenario(ROOT / "scenarios/llm-heldout.yaml")
    assert {a.pretext for a in s.actions if isinstance(a, Call)} == {"lost_phone"}
    assert {
        a.pretext for a in holdout.actions if isinstance(a, Call) and a.request == "mfa_reset"
    } == {"replacement_device"}


def test_actor_and_presentation_changes_are_rejected(world):
    approved = next(p for p in plans_for(world) if any(t.stage == "approval" for t in p.turns))
    execute = next(t for t in approved.turns if t.stage == "execute")
    assert validate_turn(execute, "I will reset MFA now and enroll the replacement factor.")
    recovered = next(t for t in approved.turns if t.stage == "recovered")
    assert validate_turn(recovered, "Thanks for your help.")
    intro = approved.turns[0]
    assert validate_turn(
        intro,
        intro.template_text.replace(intro.required_literals[0], f"[{intro.required_literals[0]}]"),
    )


def test_late_migration_approval_preserves_event_and_arrival_times(world, tmp_path):
    import json

    from pretextforge.models import Files

    action = Files(
        id="approved-long-migration",
        user=world.users[1].id,
        at=world.actions[0].at,
        count=1000,
        approved_migration=True,
    )
    prior_notice = Files(
        id="earlier-departure", user=action.user, at=action.at - timedelta(hours=2), count=1
    )
    s = world.model_copy(update={"actions": [prior_notice, action], "observation": "degraded"})
    bundle = tmp_path / "bundle"
    generate(s, bundle)
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    cid = next(c["correlation_id"] for c in truth["cases"] if c["case_id"] == action.id)
    from pretextforge.quality import read_jsonl

    grants = [
        r
        for r in read_jsonl(bundle / "logs/itsm.jsonl")
        if r["data"]["action"] == "approval_granted"
    ]
    assert len(grants) == 1
    files = [r for r in read_jsonl(bundle / "logs/saas_files.jsonl") if r["correlation_id"] == cid]
    assert all(grants[0]["event_time"] < r["event_time"] for r in files)
    assert any(r["available_at"] < grants[0]["available_at"] for r in files)
    assert any(r["available_at"] > grants[0]["available_at"] for r in files)


def test_reviewed_role_and_meaning_failures_are_rejected(world):
    plan = plans_for(world)[0]
    ask = next(t for t in plan.turns if t.stage == "ask_verification")
    assert validate_turn(ask, "Could you ask your manager to approve the recovery?")
    close = next(t for t in plan.turns if t.stage == "close")
    assert validate_turn(close, "Thanks.")
    urgency = next(t for t in plan.turns if t.stage == "urgency")
    assert validate_turn(urgency, "I cannot get to the documents as soon as I have meetings.")
    intro = plan.turns[0]
    name = intro.required_literals[0]
    assert validate_turn(intro, f"Hello, this is {name}. I need to reset MFA on my missing phone.")


def test_caller_manager_and_factor_destination_are_not_reassigned(world):
    plans = plans_for(world)
    accept = next(t for p in plans for t in p.turns if t.stage == "accept")
    phone = next(v for v in accept.required_literals if v.startswith("+"))
    assert validate_turn(accept, f"Please reach your manager. The replacement number is {phone}.")
    assert validate_turn(accept, f"Please contact my manager. The replacement factor is {phone}.")
