"""Bundle checks: schemas, causality, observation fidelity and private-field leakage."""

import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .engine import arrival_key, digest, public_reference, stable_seed, timestamp
from .models import SOURCES, Call, Record, Scenario


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(bundle: Path) -> dict:
    """Read the bundle afresh; never trust a previously saved quality result."""
    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    try:
        truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
        manifest = json.loads((bundle / "MANIFEST.json").read_text())
        scenario = Scenario.model_validate_json((bundle / "resolved_scenario.json").read_text())
        reference = json.loads((bundle / "reference.json").read_text())
        decisions = json.loads((bundle / "OBSERVATION_MANIFEST.json").read_text())
        observed = []
        for source in SOURCES:
            rows = read_jsonl(bundle / "logs" / f"{source}.jsonl")
            for row in rows:
                check(row.get("source") == source, f"wrong source in {source}.jsonl")
            check(rows == sorted(rows, key=arrival_key), f"unordered {source}.jsonl")
            observed += rows
        canonical = truth["records"]
        for row in canonical + observed:
            Record.model_validate(row)
        expected_files = {
            "GROUND_TRUTH.json",
            "GROUND_TRUTH.md",
            "resolved_scenario.json",
            "reference.json",
            "OBSERVATION_MANIFEST.json",
            "labels/cases.jsonl",
            *(f"logs/{s}.jsonl" for s in SOURCES),
        }
        if manifest.get("rendering", {}).get("mode") == "cached_llm":
            from .conversations import (
                ConversationPlan,
                content_hash,
                quality_summary,
                semantic_plan,
            )

            expected_files.add("TURN_PLANS.json")
            plans = [
                ConversationPlan.model_validate(p)
                for p in json.loads((bundle / "TURN_PLANS.json").read_text())
            ]
            text_by_cid = defaultdict(list)
            for row in canonical:
                if row["source"] == "transcripts":
                    text_by_cid[row["correlation_id"]].append(row["data"]["text"])
            check(quality_summary(plans, text_by_cid)["passed"], "rendered text/plan mismatch")
            check(set(text_by_cid) == {p.conversation_id for p in plans}, "plan coverage mismatch")
            transcript_rows = {r["record_id"]: r for r in canonical if r["source"] == "transcripts"}
            planned_ids = [turn.record_id for plan in plans for turn in plan.turns]
            check(len(planned_ids) == len(set(planned_ids)), "duplicate planned turn ids")
            check(set(planned_ids) == set(transcript_rows), "planned/public turn coverage mismatch")
            for plan in plans:
                for turn in plan.turns:
                    row = transcript_rows.get(turn.record_id)
                    check(
                        row is not None
                        and row["correlation_id"] == plan.conversation_id
                        and row["event_time"] == turn.event_time
                        and row["data"]["user"] == plan.user
                        and row["data"]["channel"] == plan.channel
                        and row["data"]["speaker"] == turn.speaker
                        and row["data"]["turn_index"] == turn.index,
                        "planned/public turn metadata mismatch",
                    )
                actual = content_hash(
                    {
                        "plan": semantic_plan(plan),
                        "config": manifest["rendering"]["config"],
                        "texts": text_by_cid[plan.conversation_id],
                    }
                )
                check(
                    actual == manifest["rendering"]["entries"].get(plan.conversation_id),
                    "rendering provenance mismatch",
                )
        check(
            set(manifest["files"]) == expected_files,
            "manifest must enumerate exactly the bundle input files",
        )
        for name, expected in manifest["files"].items():
            if name not in expected_files:
                continue
            check(digest(bundle / name) == expected, f"digest mismatch: {name}")
        check(
            {p.name for p in (bundle / "logs").iterdir()} == {f"{s}.jsonl" for s in SOURCES},
            "unexpected or missing log file",
        )
        check(reference["policy"] == scenario.policy.model_dump(), "reference policy mismatch")
        check(reference == public_reference(scenario), "reference directory/policy mismatch")
        ids = [r["record_id"] for r in canonical]
        check(canonical == sorted(canonical, key=arrival_key), "unordered canonical records")
        check(len(ids) == len(set(ids)), "duplicate canonical record ids")
        check(len(observed) == len({r["record_id"] for r in observed}), "duplicate observed ids")
        check(
            len(decisions) == len({d["record_id"] for d in decisions}),
            "duplicate observation decisions",
        )
        check(set(ids) == {d["record_id"] for d in decisions}, "observation coverage mismatch")
        lookup = {r["record_id"]: r for r in canonical}
        visible = {r["record_id"]: r for r in observed}
        groups: dict[str, list[dict]] = defaultdict(list)
        observation_groups: dict[tuple, set] = defaultdict(set)
        for row in canonical:
            check(
                scenario.start <= datetime.fromisoformat(row["event_time"]) < scenario.end,
                f"event outside window: {row['record_id']}",
            )
            groups[row["correlation_id"]].append(row)
            check(
                not (
                    {"true_actor", "malicious", "class", "case_id", "technique", "act"}
                    & (set(row) | set(row["data"]))
                ),
                "private field leaked into logs",
            )
        for decision in decisions:
            original = lookup.get(decision["record_id"])
            if original is None:
                continue
            check(
                decision["source"] == original["source"]
                and decision["correlation_id"] == original["correlation_id"],
                "observation source/correlation mismatch",
            )
            delta = (
                datetime.fromisoformat(decision["available_at"])
                - datetime.fromisoformat(original["event_time"])
            ).total_seconds()
            check(delta == decision["delay_seconds"] and delta >= 0, "invalid observation delay")
            observation_groups[(original["source"], original["correlation_id"])].add(
                (decision["dropped"], decision["delay_seconds"])
            )
            actual = visible.get(original["record_id"])
            if decision["dropped"]:
                check(actual is None, "dropped record present")
            else:
                check(
                    actual == {**original, "available_at": decision["available_at"]},
                    f"observed record differs: {original['record_id']}",
                )
        check(set(visible) <= set(lookup), "unknown observed records")
        check(
            all(len(v) == 1 for v in observation_groups.values()),
            "incoherent source/lifecycle observation",
        )
        for (source, cid), actual in observation_groups.items():
            expected_drop = (
                scenario.observation == "degraded"
                and source in {"transcripts", "telephony"}
                and (
                    random.Random(
                        stable_seed(f"{scenario.seed}:observation:{source}:{cid}")
                    ).random()
                    < 0.25
                )
            )
            expected_delay = (
                {"itsm": 300, "hr": 900}.get(source, 0) if scenario.observation == "degraded" else 0
            )
            check(
                actual == {(expected_drop, expected_delay)},
                "observation differs from configured profile",
            )
        users = {u.id: u for u in scenario.users}
        directory = {u["id"]: u for u in reference["users"]}
        active_factors = {u["id"]: u["initial_factor_id"] for u in reference["users"]}
        sessions = {}
        objects = {}
        for row in sorted(canonical, key=arrival_key):
            data, source = row["data"], row["source"]
            user = data["user"]
            if source == "idp":
                kind = data["event_type"]
                check(
                    data["actor"]
                    == (
                        scenario.helpdesk_agent if kind in {"mfa_reset", "factor_remove"} else user
                    ),
                    "identity-event actor mismatch",
                )
                if kind == "mfa_reset":
                    check(
                        data["factor_id"] == active_factors[user],
                        "reset references inactive factor",
                    )
                if kind == "factor_remove":
                    check(data["factor_id"] == active_factors[user], "removed inactive factor")
                    active_factors[user] = None
                elif kind == "factor_enroll":
                    check(
                        active_factors[user] is None and bool(data["factor_id"]),
                        "enrollment without factor removal",
                    )
                    active_factors[user] = data["factor_id"]
                elif kind == "login":
                    check(
                        bool(data["session_id"]) and bool(data["factor_id"]),
                        "login lacks session/factor",
                    )
                    check(
                        data["factor_id"] == active_factors[user]
                        and active_factors[user] is not None,
                        "login uses inactive factor",
                    )
                    check(
                        data["device_id"] == directory[user]["device_id"]
                        and data["src_ip"] == directory[user]["src_ip"],
                        "login context mismatch",
                    )
                    check(data["session_id"] not in sessions, "duplicate authenticated session")
                    sessions[data["session_id"]] = row
            elif source == "saas_files":
                session = sessions.get(data["session_id"])
                check(session is not None, "file activity has no earlier authenticated session")
                if session:
                    check(
                        all(
                            data[k] == session["data"][k]
                            for k in ("user", "session_id", "device_id", "src_ip")
                        ),
                        "file/session context mismatch",
                    )
                    check(session["event_time"] < row["event_time"], "file activity precedes login")
                    check(
                        session["correlation_id"] == row["correlation_id"],
                        "file/session lifecycle mismatch",
                    )
                key = (user, data["object_id"])
                if data["action"] == "download":
                    check(key not in objects or objects[key] == data["bytes"], "unstable file size")
                    objects[key] = data["bytes"]
                else:
                    check(objects.get(key) == data["bytes"], "share has no matching prior download")
        for cid, rows in groups.items():
            rows = sorted(rows, key=lambda r: (r["event_time"], r["record_id"]))
            events = [
                (r["source"], r["data"].get("event_type", r["data"].get("action"))) for r in rows
            ]
            check(len({r["data"]["user"] for r in rows}) == 1, f"mixed users in lifecycle: {cid}")
            for row in rows:
                check(row["data"]["user"] in users, f"unknown record user: {cid}")
                if row["source"] == "itsm" and row["data"]["action"] == "approval_granted":
                    if row["data"]["category"] == "mfa_reset":
                        check(
                            row["data"]["approver"] == users[row["data"]["user"]].manager,
                            f"wrong approving manager: {cid}",
                        )
                    else:
                        check(
                            row["data"]["approver"] == scenario.helpdesk_agent,
                            f"wrong migration approver: {cid}",
                        )
            if ("idp", "mfa_reset") in events:
                needed = [
                    ("idp", "mfa_reset"),
                    ("idp", "factor_remove"),
                    ("itsm", "reset"),
                    ("idp", "factor_enroll"),
                    ("idp", "login"),
                ]
                check(
                    all(events.count(e) == 1 for e in needed), f"reset lifecycle incomplete: {cid}"
                )
                if all(e in events for e in needed):
                    check(
                        [events.index(e) for e in needed]
                        == sorted(events.index(e) for e in needed),
                        f"reset order invalid: {cid}",
                    )
                    enroll = next(
                        r
                        for r in rows
                        if (r["source"], r["data"].get("event_type")) == ("idp", "factor_enroll")
                    )
                    login = next(
                        r
                        for r in rows
                        if (r["source"], r["data"].get("event_type")) == ("idp", "login")
                    )
                    check(
                        enroll["data"]["factor_id"] == login["data"]["factor_id"],
                        f"enrollment/login factor mismatch: {cid}",
                    )
            if ("itsm", "opened") in events:
                check(events.count(("itsm", "opened")) == 1, f"duplicate ticket opening: {cid}")
                check(events.count(("itsm", "closed")) == 1, f"unclosed ticket: {cid}")
                check(
                    len({r["data"]["ticket_id"] for r in rows if r["source"] == "itsm"}) == 1,
                    f"inconsistent ticket ids: {cid}",
                )
                check(
                    events.index(("itsm", "opened")) < events.index(("itsm", "closed"))
                    if ("itsm", "closed") in events
                    else False,
                    f"ticket timing invalid: {cid}",
                )
            turns = [r for r in rows if r["source"] == "transcripts"]
            channels = {r["data"]["channel"] for r in turns}
            check(len(channels) <= 1, f"mixed transcript channels: {cid}")
            phone_count = sum(r["source"] == "telephony" for r in rows)
            if turns:
                check(
                    phone_count == (2 if channels == {"call"} else 0),
                    f"channel/telephony mismatch: {cid}",
                )
            previous_end = None
            for turn in turns:
                data = turn["data"]
                end = datetime.fromisoformat(turn["event_time"])
                start = end - timedelta(seconds=data["duration_s"])
                check(data["conversation_id"] == cid, f"transcript conversation mismatch: {cid}")
                words = len(data["text"].split())
                check(
                    words > 0
                    and max(1, math.ceil(words * 60 / 175))
                    <= data["duration_s"]
                    <= max(1, math.ceil(words * 60 / 115)),
                    f"implausible speech duration: {cid}",
                )
                if previous_end:
                    check(
                        1 <= (start - previous_end).total_seconds() - data["wait_before_s"] <= 3,
                        f"speech/wait overlap: {cid}",
                    )
                check(bool(data["wait_reason"]) == bool(data["wait_before_s"]), "unexplained wait")
                previous_end = end
            check(
                [r["data"]["turn_index"] for r in turns] == list(range(1, len(turns) + 1)),
                f"transcript turn order invalid: {cid}",
            )
            if ("telephony", "started") in events:
                check(events.count(("telephony", "started")) == 1, f"duplicate call start: {cid}")
                check(events.count(("telephony", "ended")) == 1, f"missing call end: {cid}")
                legs = [r for r in rows if r["source"] == "telephony"]
                check(len(legs) == 2, f"invalid number of call legs: {cid}")
                if len(legs) == 2:
                    check(
                        all(r["data"]["call_id"] == cid for r in legs), f"call id mismatch: {cid}"
                    )
                    check(
                        legs[0]["data"]["to_number"] == scenario.helpdesk_number,
                        "call targets an unconfigured service number",
                    )
                    check(
                        all(
                            legs[0]["event_time"] <= t["event_time"] <= legs[1]["event_time"]
                            for t in turns
                        ),
                        "transcript outside phone call",
                    )
                    check(
                        all(
                            datetime.fromisoformat(t["event_time"])
                            - timedelta(seconds=t["data"]["duration_s"])
                            >= datetime.fromisoformat(legs[0]["event_time"])
                            for t in turns
                        ),
                        "speech starts before phone call",
                    )
                    check(legs[0]["data"]["duration_s"] is None, "call duration exposed early")
                    elapsed = (
                        datetime.fromisoformat(legs[1]["event_time"])
                        - datetime.fromisoformat(legs[0]["event_time"])
                    ).total_seconds()
                    check(elapsed == legs[1]["data"]["duration_s"], f"wrong call duration: {cid}")
                    check(
                        all(
                            legs[0]["data"][k] == legs[1]["data"][k]
                            for k in ("call_id", "from_number", "to_number")
                        ),
                        f"inconsistent call identity: {cid}",
                    )
                if ("itsm", "opened") in events and ("itsm", "closed") in events:
                    check(
                        events.index(("telephony", "started"))
                        < events.index(("itsm", "opened"))
                        < events.index(("telephony", "ended"))
                        < events.index(("itsm", "closed"))
                        if ("telephony", "ended") in events
                        else False,
                        f"call/ticket timing invalid: {cid}",
                    )
        check(read_jsonl(bundle / "labels/cases.jsonl") == truth["cases"], "case labels differ")
        check(
            len({c["case_id"] for c in truth["cases"]}) == len(truth["cases"]), "duplicate case ids"
        )
        case_index = {c["case_id"]: c for c in truth["cases"]}
        for action in scenario.actions:
            case = case_index.get(action.id)
            check(case is not None, f"missing scripted case: {action.id}")
            if case is not None:
                linked = groups.get(case["correlation_id"], [])
                check(
                    all(r["data"]["user"] == action.user for r in linked),
                    f"case/record user mismatch: {action.id}",
                )
                resets = [
                    r
                    for r in linked
                    if r["source"] == "idp" and r["data"]["event_type"] == "mfa_reset"
                ]
                ticket_actions = [r["data"]["action"] for r in linked if r["source"] == "itsm"]
                if isinstance(action, Call):
                    recovery = action.request == "mfa_reset"
                    check(
                        len(resets) == int(recovery and action.execute),
                        f"reset outcome mismatch: {action.id}",
                    )
                    expected_actions = ["opened"]
                    if recovery and action.verification == "approved":
                        expected_actions += ["approval_requested", "approval_granted"]
                    if recovery:
                        expected_actions += ["reset" if action.execute else "denied"]
                    expected_actions += ["closed"]
                    check(
                        ticket_actions == expected_actions,
                        f"ticket lifecycle mismatch: {action.id}",
                    )
                    check(
                        all(
                            r["data"]["category"] == action.request
                            for r in linked
                            if r["source"] == "itsm"
                        ),
                        f"ticket category mismatch: {action.id}",
                    )
                    check(
                        case["action_point"] == (resets[0]["event_time"] if resets else None),
                        f"reset action-point mismatch: {action.id}",
                    )
                else:
                    downloads = [
                        r
                        for r in linked
                        if r["source"] == "saas_files" and r["data"]["action"] == "download"
                    ]
                    shares = [
                        r
                        for r in linked
                        if r["source"] == "saas_files" and r["data"]["action"] == "share_external"
                    ]
                    check(
                        len(downloads) == action.count and len(shares) == 1,
                        f"file count mismatch: {action.id}",
                    )
                    check(
                        len([r for r in linked if r["source"] == "hr"]) == int(action.departure),
                        f"departure outcome mismatch: {action.id}",
                    )
                    check(
                        ticket_actions
                        == (
                            ["opened", "approval_requested", "approval_granted", "closed"]
                            if action.approved_migration
                            else []
                        ),
                        f"migration approval mismatch: {action.id}",
                    )
                    check(
                        all(
                            r["data"]["category"] == "migration"
                            for r in linked
                            if r["source"] == "itsm"
                        ),
                        f"migration category mismatch: {action.id}",
                    )
                    if shares and downloads:
                        check(
                            shares[0]["data"]["object_id"] == downloads[0]["data"]["object_id"]
                            and shares[0]["data"]["recipient"] == action.recipient
                            and case["action_point"] == shares[0]["event_time"],
                            f"share facts mismatch: {action.id}",
                        )
                satisfied = (
                    action.request == "vpn" or action.verification == "approved"
                    if isinstance(action, Call)
                    else action.approved_migration
                )
                check(
                    case["malicious"] == action.malicious
                    and case["user"] == action.user
                    and case["procedure_satisfied"] == satisfied,
                    f"scenario/label mismatch: {action.id}",
                )
        for case in truth["cases"]:
            linked = groups.get(case["correlation_id"], [])
            check(
                all(r["data"]["user"] == case["user"] for r in linked),
                f"case/record user mismatch: {case['case_id']}",
            )
            if case["kind"] == "helpdesk":
                ticket_actions = [r["data"]["action"] for r in linked if r["source"] == "itsm"]
                check(
                    ticket_actions.count("opened") == 1 and ticket_actions.count("closed") == 1,
                    f"missing helpdesk ticket lifecycle: {case['case_id']}",
                )
                logins = [
                    r for r in linked if r["source"] == "idp" and r["data"]["event_type"] == "login"
                ]
                if logins:
                    recovery = lookup.get(case.get("recovery_confirmation_record_id"))
                    check(
                        recovery is not None
                        and recovery["source"] == "transcripts"
                        and recovery["data"]["speaker"] == "caller"
                        and recovery["correlation_id"] == case["correlation_id"]
                        and datetime.fromisoformat(recovery["event_time"])
                        - timedelta(seconds=recovery["data"]["duration_s"])
                        > datetime.fromisoformat(logins[0]["event_time"]),
                        "spoken recovery precedes successful login",
                    )
            request_id = case.get("verification_request_record_id")
            if request_id:
                consent = lookup[case["verification_consent_record_id"]]
                spoken = lookup[request_id]
                confirm = lookup[case["approval_confirmation_record_id"]]
                request = next(
                    (
                        r
                        for r in linked
                        if r["source"] == "itsm" and r["data"]["action"] == "approval_requested"
                    ),
                    None,
                )
                grant = next(
                    (
                        r
                        for r in linked
                        if r["source"] == "itsm" and r["data"]["action"] == "approval_granted"
                    ),
                    None,
                )
                check(
                    request is not None
                    and grant is not None
                    and consent["event_time"]
                    < spoken["event_time"]
                    < request["event_time"]
                    < grant["event_time"]
                    < timestamp(
                        datetime.fromisoformat(confirm["event_time"])
                        - timedelta(seconds=confirm["data"]["duration_s"])
                    ),
                    "spoken approval chronology mismatch",
                )
            check(
                set(case["record_ids"]) == {r["record_id"] for r in linked} and bool(linked),
                f"invalid record links: {case['case_id']}",
            )
            evidence = case["first_evidence_record_id"]
            check(evidence is None or evidence in case["record_ids"], "invalid evidence label")
            if case["action_point"]:
                check(
                    any(r["event_time"] == case["action_point"] for r in linked),
                    "action point has no record",
                )
            if case["procedure_satisfied"] and case["action_point"]:
                approvals = [
                    r
                    for r in linked
                    if r["source"] == "itsm" and r["data"]["action"] == "approval_granted"
                ]
                check(
                    bool(approvals) and approvals[0]["event_time"] < case["action_point"],
                    f"verified action precedes approval: {case['case_id']}",
                )
        counts = dict(sorted(Counter(r["source"] for r in observed).items()))
        return {
            "passed": not errors,
            "errors": errors,
            "canonical_records": len(canonical),
            "observed_records": len(observed),
            "dropped_records": len(canonical) - len(observed),
            "records_per_source": counts,
            "cases": len(truth["cases"]),
            "attacks": sum(c["malicious"] for c in truth["cases"]),
            "checks": [
                "schemas",
                "integrity",
                "causality",
                "timing",
                "references",
                "observation",
                "labels",
                "private_field_leakage",
                "active_factor_and_session_joins",
                "spoken_approval_and_recovery_chronology",
                "configured_observation_profile",
                "scenario_outcomes_and_participants",
                "speech_duration_and_plan_clocks",
            ],
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"passed": False, "errors": [*errors, f"invalid bundle: {exc}"]}
