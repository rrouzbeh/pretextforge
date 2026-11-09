"""Action-owned expansion: shared facts are computed once, sources only serialize."""

import hashlib
import json
import math
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .conversations import ConversationPlan, TurnPlan, validate_turn
from .models import SOURCES, Call, Files, Record, Scenario, timestamp


def stable_seed(scope: str) -> int:
    return int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big")


def opaque(scope: str) -> str:
    return hashlib.sha256(scope.encode()).hexdigest()[:24]


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arrival_key(row: dict) -> tuple:
    # Preserve causal event order when delayed records become available together.
    return (
        datetime.fromisoformat(row["available_at"]),
        datetime.fromisoformat(row["event_time"]),
        row["record_id"],
    )


class Builder:
    def __init__(self, scenario: Scenario, transcripts: dict | None = None):
        self.scenario = scenario
        self.transcripts = transcripts or {}
        self.contexts = identity_contexts(scenario)
        self.catalog_weights = [1 / (i + 1) for i in range(80)]
        self.records: list[dict] = []
        self.cases: list[dict] = []
        self.plans: list[ConversationPlan] = []
        self.counts: dict[tuple[str, str], int] = {}
        self.users = {u.id: u for u in scenario.users}
        self.factors = {
            u.id: opaque(f"{scenario.seed}:initial-factor:{u.id}") for u in scenario.users
        }
        template_path = Path(__file__).with_name("templates.yaml")
        self.templates = yaml.safe_load(template_path.read_text())
        self.template_digest = digest(template_path)

    def identifier(self, scope: str) -> str:
        return opaque(f"{self.scenario.seed}:{scope}")

    def add(self, cid: str, source: str, at: datetime, **data: object) -> dict:
        key = (cid, source)
        index = self.counts.get(key, 0)
        self.counts[key] = index + 1
        row = Record(
            record_id=self.identifier(f"{cid}:{source}:{index}"),
            source=source,
            event_time=at,
            available_at=at,
            correlation_id=cid,
            data=data,
        ).model_dump(mode="json")
        # Normalize timestamps once so lexical sorting and public formatting agree.
        row["event_time"] = timestamp(at)
        row["available_at"] = timestamp(at)
        self.records.append(row)
        return row

    def login(self, cid: str, at: datetime, user: str, factor_id: str | None = None) -> dict:
        return self.add(
            cid,
            "idp",
            at,
            event_type="login",
            actor=user,
            user=user,
            factor_id=factor_id,
            session_id=self.identifier(f"{cid}:session"),
            **self.contexts[user],
        )

    def call(self, action: Call, *, routine: bool = False) -> None:
        cid = self.identifier(f"{'routine' if routine else 'action'}:{action.id}")
        user = self.users[action.user]
        rng = random.Random(stable_seed(f"{self.scenario.seed}:dialogue:{cid}"))
        ticket = self.identifier(f"{cid}:ticket")
        phone = action.caller_number or user.phone
        cursor = action.at + timedelta(seconds=rng.randint(2, 5))
        stages = []

        def line(kind, speaker, acts):
            stages.append((kind, speaker, acts))

        if action.channel == "call":
            self.add(
                cid,
                "telephony",
                action.at,
                call_id=cid,
                user=user.id,
                event_type="started",
                from_number=phone,
                to_number=self.scenario.helpdesk_number,
                duration_s=None,
            )
        self.add(
            cid,
            "itsm",
            action.at + timedelta(seconds=1),
            ticket_id=ticket,
            user=user.id,
            action="opened",
            category=action.request,
        )
        line(
            "vpn_open" if action.request == "vpn" else f"mfa_open_{action.pretext}",
            "caller",
            [
                "identity_claim",
                "troubleshooting" if action.request == "vpn" else "sensitive_request",
            ],
        )
        if action.request == "mfa_reset":
            line("policy", "agent", ["offer_verification"])
        else:
            line("vpn_steps", "agent", ["troubleshooting"])
        if action.urgent:
            line("urgency", "caller", ["urgency"])
        if action.request == "mfa_reset":
            if action.verification == "approved":
                line("accept", "caller", ["accept_verification", "destination_change"])
                line("request_approval", "agent", ["offer_verification"])
                line("approval", "agent", ["approval_claim"])
            else:
                line("ask_verification", "agent", ["offer_verification"])
                line(
                    "bypass" if action.verification == "refused" else "destination",
                    "caller",
                    (["bypass_request"] if action.verification == "refused" else [])
                    + ["destination_change"],
                )
                line("missing", "agent", ["verification_missing"])
            line(
                "execute" if action.execute else "deny",
                "agent",
                ["action_commit" if action.execute else "action_hold"],
            )
            line("recovered" if action.execute else "denied_closing", "caller", ["closing"])
        else:
            line("vpn_fixed", "caller", ["closing"])
        line("close", "agent", ["closing"])
        first_evidence = action_ts = request_at = granted_at = login_at = None
        request_record = confirmation_record = consent_record = recovery_record = None
        planned = []
        supplied = self.transcripts.get(cid)
        if supplied is not None and len(supplied) != len(stages):
            raise ValueError("rendered conversation has the wrong number of turns")
        for i, (kind, speaker, acts) in enumerate(stages):
            template = rng.choice(self.templates[kind]).format(
                name=user.name, number=action.new_number
            )
            text = supplied[i] if supplied is not None else template
            # Independent scoped timing RNG: wording never changes phrase selection.
            clock = random.Random(stable_seed(f"{self.scenario.seed}:speech:{cid}:{i}"))
            pause = clock.randint(1, 3)
            wait, reason = 0, None
            if kind == "approval":
                wait = max(0, math.ceil((granted_at - cursor).total_seconds())) + 1
                reason = "manager_approval"
            elif kind == "recovered":
                wait = max(0, math.ceil((login_at - cursor).total_seconds())) + 1
                reason = "factor_enrollment"
            elif kind == "vpn_fixed":
                wait, reason = clock.randint(8, 25), "client_restart"
            duration = max(1, math.ceil(len(text.split()) * 60 / clock.triangular(115, 175, 145)))
            started = cursor + timedelta(seconds=pause + wait)
            ended = started + timedelta(seconds=duration)
            row = self.add(
                cid,
                "transcripts",
                ended,
                conversation_id=cid,
                user=user.id,
                channel=action.channel,
                speaker=speaker,
                turn_index=i + 1,
                text=text,
                duration_s=duration,
                wait_before_s=wait,
                wait_reason=reason,
            )
            literals = [
                value
                for marker, value in (("{name}", user.name), ("{number}", action.new_number))
                if marker in self.templates[kind][0]
            ]
            # All variants in a bank carry the same authoritative slots.
            planned.append(
                TurnPlan(
                    record_id=row["record_id"],
                    index=i + 1,
                    speaker=speaker,
                    event_time=row["event_time"],
                    acts=acts,
                    template_text=template,
                    required_literals=literals,
                    stage=kind,
                )
            )
            errors = validate_turn(planned[-1], text)
            if errors:
                raise ValueError(f"invalid {kind} utterance: {'; '.join(errors)}")
            cursor = ended
            if kind == "request_approval":
                request_record = row["record_id"]
                request_at = ended + timedelta(seconds=1)
                granted_at = request_at + timedelta(seconds=clock.randint(20, 65))
                for when, event in (
                    (request_at, "approval_requested"),
                    (granted_at, "approval_granted"),
                ):
                    self.add(
                        cid,
                        "itsm",
                        when,
                        ticket_id=ticket,
                        user=user.id,
                        action=event,
                        category="mfa_reset",
                        approver=user.manager,
                    )
            if kind == "approval":
                confirmation_record = row["record_id"]
            if kind == "accept":
                consent_record = row["record_id"]
            if kind == "recovered":
                recovery_record = row["record_id"]
            if (
                kind in {"bypass", "execute"}
                and action.verification != "approved"
                and first_evidence is None
            ):
                first_evidence = row["record_id"]
            if kind == "execute":
                reset = ended + timedelta(seconds=1)
                enrolled = reset + timedelta(seconds=clock.randint(12, 25))
                login_at = enrolled + timedelta(seconds=clock.randint(4, 10))
                new_factor = self.identifier(f"{cid}:factor")
                admin = {"device_id": "device-service-desk", "src_ip": "10.254.0.1"}
                for when, event in (
                    (reset, "mfa_reset"),
                    (reset + timedelta(seconds=1), "factor_remove"),
                ):
                    self.add(
                        cid,
                        "idp",
                        when,
                        event_type=event,
                        actor=self.scenario.helpdesk_agent,
                        user=user.id,
                        factor_id=self.factors[user.id],
                        **admin,
                    )
                self.add(
                    cid,
                    "itsm",
                    reset + timedelta(seconds=2),
                    ticket_id=ticket,
                    user=user.id,
                    action="reset",
                    category="mfa_reset",
                )
                self.add(
                    cid,
                    "idp",
                    enrolled,
                    event_type="factor_enroll",
                    actor=user.id,
                    user=user.id,
                    factor_id=new_factor,
                    destination=action.new_number,
                    **self.contexts[user.id],
                )
                self.login(cid, login_at, user.id, new_factor)
                self.factors[user.id] = new_factor
                action_ts = timestamp(reset)
            elif kind == "deny":
                self.add(
                    cid,
                    "itsm",
                    ended + timedelta(seconds=1),
                    ticket_id=ticket,
                    user=user.id,
                    action="denied",
                    category="mfa_reset",
                )
        end = cursor + timedelta(seconds=2)
        if action.channel == "call":
            self.add(
                cid,
                "telephony",
                end,
                call_id=cid,
                user=user.id,
                event_type="ended",
                from_number=phone,
                to_number=self.scenario.helpdesk_number,
                duration_s=int((end - action.at).total_seconds()),
            )
        self.add(
            cid,
            "itsm",
            end + timedelta(seconds=2),
            ticket_id=ticket,
            user=user.id,
            action="closed",
            category=action.request,
        )
        self.plans.append(
            ConversationPlan(
                conversation_id=cid,
                user=user.id,
                channel=action.channel,
                style="natural and concise",
                turns=planned,
            )
        )
        self.cases.append(
            {
                "case_id": action.id,
                "correlation_id": cid,
                "user": user.id,
                "kind": "helpdesk",
                "malicious": action.malicious,
                "class": "attack"
                if action.malicious
                else ("routine" if routine or action.request == "vpn" else "legitimate_twin"),
                "true_actor": action.true_actor or user.id,
                "procedure_satisfied": action.request == "vpn" or action.verification == "approved",
                "first_evidence_record_id": first_evidence,
                "action_point": action_ts,
                "verification_request_record_id": request_record,
                "approval_confirmation_record_id": confirmation_record,
                "verification_consent_record_id": consent_record,
                "recovery_confirmation_record_id": recovery_record,
            }
        )

    def files(self, action: Files) -> None:
        cid = self.identifier(f"action:{action.id}")
        ticket = self.identifier(f"{cid}:ticket")
        at = action.at
        if action.departure:
            self.add(
                cid,
                "hr",
                at,
                user=action.user,
                status="notice_given",
                last_day=timestamp(at + timedelta(days=14)),
            )
        if action.approved_migration:
            for seconds, kind in (
                (10, "opened"),
                (20, "approval_requested"),
                (60, "approval_granted"),
            ):
                self.add(
                    cid,
                    "itsm",
                    at + timedelta(seconds=seconds),
                    ticket_id=ticket,
                    user=action.user,
                    action=kind,
                    category="migration",
                    approver=self.scenario.helpdesk_agent,
                )
        self.login(cid, at + timedelta(seconds=90), action.user)
        file_rng = random.Random(stable_seed(f"{self.scenario.seed}:files:{cid}"))
        elapsed = 120.0
        sizes = []
        for i in range(action.count):
            sizes.append(int(file_rng.lognormvariate(10, 1.1)))
            elapsed += file_rng.triangular(0.2, 1.7, 0.7)
            self.add(
                cid,
                "saas_files",
                at + timedelta(seconds=elapsed),
                user=action.user,
                action="download",
                object_id=self.identifier(f"{cid}:file:{i}"),
                bytes=sizes[-1],
                session_id=self.identifier(f"{cid}:session"),
                **self.contexts[action.user],
                ticket_id=ticket if action.approved_migration else None,
            )
        row = self.add(
            cid,
            "saas_files",
            at + timedelta(seconds=elapsed + 5),
            user=action.user,
            action="share_external",
            object_id=self.identifier(f"{cid}:file:0"),
            bytes=sizes[0],
            session_id=self.identifier(f"{cid}:session"),
            **self.contexts[action.user],
            recipient=action.recipient,
            ticket_id=ticket if action.approved_migration else None,
        )
        if action.approved_migration:
            self.add(
                cid,
                "itsm",
                at + timedelta(seconds=elapsed + 10),
                ticket_id=ticket,
                user=action.user,
                action="closed",
                category="migration",
            )
        self.cases.append(
            {
                "case_id": action.id,
                "correlation_id": cid,
                "user": action.user,
                "kind": "files",
                "malicious": action.malicious,
                "class": "attack" if action.malicious else "legitimate_twin",
                "true_actor": action.user,
                "procedure_satisfied": action.approved_migration,
                "first_evidence_record_id": None,
                "action_point": row["event_time"],
            }
        )

    def build(self) -> tuple[list[dict], list[dict]]:
        scenario = self.scenario
        calls: list[tuple[Call, bool]] = []
        for day in range(scenario.workdays):
            day_start = scenario.start + timedelta(days=day)
            for user in scenario.users:
                rng = random.Random(stable_seed(f"{scenario.seed}:baseline:{day}:{user.id}"))
                bias = random.Random(stable_seed(f"{scenario.seed}:workload:{user.id}")).uniform(
                    0.65, 1.35
                )
                day_bias = (0.95, 1.1, 1.0, 1.05, 0.8)[day_start.weekday()]

                def count(mean, rng=rng, bias=bias, day_bias=day_bias):
                    return (
                        max(0, round(rng.gauss(mean * bias * day_bias, math.sqrt(mean))))
                        if mean
                        else 0
                    )

                login_count = count(scenario.baseline.daily_logins_per_user)
                file_count = count(scenario.baseline.daily_files_per_user)
                if file_count:
                    login_count = max(1, login_count)
                sessions = []
                for i in range(login_count):
                    hour = rng.choice((1.0, 4.4, 6.0))
                    seconds = min(7 * 3600, max(300, rng.gauss(hour * 3600, 900)))
                    at = day_start + timedelta(seconds=seconds)
                    for action in sorted(scenario.actions, key=lambda a: a.at):
                        if (
                            isinstance(action, Call)
                            and action.user == user.id
                            and action.at <= at < action.at + timedelta(seconds=600)
                        ):
                            at = action.at + timedelta(seconds=601)
                    cid = self.identifier(f"baseline-session:{day}:{user.id}:{i}")
                    self.login(cid, at, user.id)
                    sessions.append((at, cid))
                for _i in range(file_count):
                    auth, cid = rng.choice(sessions)
                    at = auth + timedelta(seconds=rng.triangular(2, 1800, 180))
                    object_index = rng.choices(range(80), weights=self.catalog_weights)[0]
                    obj = self.identifier(f"catalog:{user.id}:{object_index}")
                    size_rng = random.Random(stable_seed(obj))
                    self.add(
                        cid,
                        "saas_files",
                        at,
                        user=user.id,
                        action="download",
                        object_id=obj,
                        bytes=int(size_rng.lognormvariate(10, 1.2)),
                        session_id=self.identifier(f"{cid}:session"),
                        **self.contexts[user.id],
                    )
            help_rng = random.Random(stable_seed(f"{scenario.seed}:support-count:{day}"))
            daily = (
                max(
                    0,
                    round(
                        help_rng.gauss(
                            scenario.baseline.daily_helpdesk,
                            math.sqrt(scenario.baseline.daily_helpdesk),
                        )
                    ),
                )
                if scenario.baseline.daily_helpdesk
                else 0
            )
            for i in range(daily):
                rng = random.Random(stable_seed(f"{scenario.seed}:routine:{day}:{i}"))
                user = rng.choice(scenario.users)
                calls.append(
                    (
                        Call(
                            id=f"routine-{day}-{i}",
                            user=user.id,
                            at=day_start + timedelta(seconds=600 + i * 210 + rng.randint(0, 30)),
                            channel=rng.choice(["call", "chat"]),
                            request="vpn",
                            urgent=rng.random() < 0.15,
                        ),
                        True,
                    )
                )
        calls += [(a, False) for a in scenario.actions if isinstance(a, Call)]
        # Only scripted resets update factor state; process these in chronological order.
        for call, routine in sorted(calls, key=lambda pair: (pair[0].at, pair[0].id)):
            self.call(call, routine=routine)
        for action in scenario.actions:
            if isinstance(action, Files):
                self.files(action)
        self.records.sort(key=arrival_key)
        active = {u.id: self.identifier(f"initial-factor:{u.id}") for u in scenario.users}
        for row in self.records:
            if row["source"] != "idp":
                continue
            data = row["data"]
            if data["event_type"] == "factor_remove":
                if data["factor_id"] != active[data["user"]]:
                    raise ValueError("factor removal does not match active factor")
                active[data["user"]] = None
            elif data["event_type"] == "factor_enroll":
                active[data["user"]] = data["factor_id"]
            elif data["event_type"] == "login":
                factor = active[data["user"]]
                if factor is None:
                    raise ValueError("login scheduled while factor is being replaced")
                if data["factor_id"] is not None and data["factor_id"] != factor:
                    raise ValueError("login uses a stale factor")
                data["factor_id"] = factor
        self.cases.sort(key=lambda c: c["case_id"])
        case_records = {case["correlation_id"]: [] for case in self.cases}
        for record in self.records:
            if record["correlation_id"] in case_records:
                case_records[record["correlation_id"]].append(record["record_id"])
        for case in self.cases:
            case["record_ids"] = case_records[case["correlation_id"]]
        self.label_file_evidence()
        return self.records, self.cases

    def label_file_evidence(self) -> None:
        """Derive procedural file labels from typed world events, including baseline use."""
        cases = {c["correlation_id"]: c for c in self.cases if c["kind"] == "files"}
        for case in cases.values():
            case["first_evidence_record_id"] = None
        notice = {}
        approved = set()
        recent = defaultdict(deque)
        for row in self.records:
            data = row["data"]
            user = data["user"]
            at = datetime.fromisoformat(row["event_time"])
            if row["source"] == "hr":
                notice[user] = at
            elif (
                row["source"] == "itsm"
                and data["action"] == "approval_granted"
                and data["category"] == "migration"
            ):
                approved.add((data["ticket_id"], user))
            elif row["source"] == "saas_files":
                window = recent[user]
                while window and window[0][0] < at - timedelta(hours=1):
                    window.popleft()
                if (data.get("ticket_id"), user) in approved:
                    continue
                if data["action"] == "download":
                    window.append((at, row["record_id"]))
                case = cases.get(row["correlation_id"])
                watched = user in notice and at - notice[user] <= timedelta(
                    days=self.scenario.policy.departure_watch_days
                )
                if (
                    case
                    and not case["first_evidence_record_id"]
                    and watched
                    and (
                        len(window) >= self.scenario.policy.bulk_download_threshold
                        or data["action"] == "share_external"
                    )
                ):
                    case["first_evidence_record_id"] = row["record_id"]


def observe(records: list[dict], scenario: Scenario) -> tuple[list[dict], list[dict]]:
    """One loss/delay decision per source and correlation group, independent of intent."""
    observed, decisions = [], []
    for record in records:
        row = {**record}
        source, cid = row["source"], row["correlation_id"]
        rng = random.Random(stable_seed(f"{scenario.seed}:observation:{source}:{cid}"))
        dropped = False
        delay = 0
        if scenario.observation == "degraded":
            dropped = source in {"transcripts", "telephony"} and rng.random() < 0.25
            delay = {"itsm": 300, "hr": 900}.get(source, 0)
        row["available_at"] = timestamp(
            datetime.fromisoformat(row["event_time"]) + timedelta(seconds=delay)
        )
        decisions.append(
            {
                "record_id": row["record_id"],
                "source": source,
                "correlation_id": cid,
                "dropped": dropped,
                "delay_seconds": delay,
                "available_at": row["available_at"],
            }
        )
        if not dropped:
            observed.append(row)
    return sorted(observed, key=arrival_key), decisions


def identity_contexts(scenario: Scenario) -> dict:
    return {
        u.id: {"device_id": f"device-{u.id}", "src_ip": f"10.20.{i // 250}.{i % 250 + 1}"}
        for i, u in enumerate(scenario.users)
    }


def public_reference(scenario: Scenario) -> dict:
    contexts = identity_contexts(scenario)
    return {
        "as_of": timestamp(scenario.start),
        "users": [
            {
                **u.model_dump(),
                **contexts[u.id],
                "initial_factor_id": opaque(f"{scenario.seed}:initial-factor:{u.id}"),
            }
            for u in scenario.users
        ],
        "helpdesk_agent": scenario.helpdesk_agent,
        "helpdesk_number": scenario.helpdesk_number,
        "policy": scenario.policy.model_dump(),
    }


def write_logs(directory: Path, records: list[dict]) -> None:
    directory.mkdir()
    for source in SOURCES:
        (directory / f"{source}.jsonl").write_text(
            jsonl([r for r in records if r["source"] == source])
        )
