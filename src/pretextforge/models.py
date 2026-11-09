"""Scenario and source-record contracts."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
Phone = Annotated[str, Field(pattern=r"^\+[1-9](?:[ -]?\d){6,14}$")]


class User(StrictModel):
    id: NonBlank
    name: NonBlank
    phone: Phone
    manager: str | None = None
    department: str = "operations"


class Policy(StrictModel):
    recovery_requires: Literal["manager_approval"] = "manager_approval"
    bulk_download_threshold: int = Field(default=20, ge=2)
    departure_watch_days: int = Field(default=14, ge=1)


class ActionBase(StrictModel):
    id: NonBlank
    at: datetime
    user: NonBlank
    malicious: bool = False

    @field_validator("at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("action time must be timezone-aware")
        return value.astimezone(UTC)


class Call(ActionBase):
    type: Literal["helpdesk"] = "helpdesk"
    true_actor: NonBlank | None = None
    channel: Literal["call", "chat"] = "call"
    request: Literal["mfa_reset", "vpn"] = "mfa_reset"
    verification: Literal["approved", "refused", "not_attempted"] = "approved"
    execute: bool = True
    urgent: bool = False
    caller_number: Phone | None = None
    new_number: Phone = "+1-202-555-0142"
    pretext: Literal["lost_phone", "replacement_device"] = "lost_phone"


class Files(ActionBase):
    type: Literal["files"] = "files"
    departure: bool = True
    approved_migration: bool = False
    count: int = Field(default=30, ge=1, le=10000)
    recipient: NonBlank = "personal@example.net"


Action = Annotated[Call | Files, Field(discriminator="type")]


class Baseline(StrictModel):
    daily_logins_per_user: int = Field(default=2, ge=0, le=20)
    daily_files_per_user: int = Field(default=3, ge=0, le=100)
    daily_helpdesk: int = Field(default=3, ge=0, le=100)


class Scenario(StrictModel):
    version: Literal["1"] = "1"
    name: NonBlank
    seed: int = 4242
    start: datetime
    workdays: int = Field(default=5, ge=1, le=5)
    users: list[User] = Field(min_length=1)
    helpdesk_agent: NonBlank = "agent.lee"
    helpdesk_number: Phone = "+1-210-555-0100"
    policy: Policy = Policy()
    baseline: Baseline = Baseline()
    observation: Literal["complete", "degraded"] = "complete"
    actions: list[Action] = Field(default_factory=list)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.workdays)

    @model_validator(mode="after")
    def check_world(self) -> "Scenario":
        if self.start.tzinfo is None or self.start.utcoffset() != timedelta(0):
            raise ValueError("start must be timezone-aware UTC")
        if self.start.weekday() + self.workdays > 5:
            raise ValueError("window must fit within Monday-Friday; split longer scenarios")
        ids = [u.id for u in self.users]
        if len(set(ids)) != len(ids):
            raise ValueError("user ids must be unique")
        phone_keys = {re.sub(r"\D", "", u.phone) for u in self.users}
        if len(phone_keys) != len(ids):
            raise ValueError("phone-of-record values must be unique")
        if re.sub(r"\D", "", self.helpdesk_number) in phone_keys:
            raise ValueError("helpdesk_number must not collide with an employee phone")
        if self.helpdesk_agent in ids:
            raise ValueError("helpdesk_agent must be distinct from users")
        for user in self.users:
            if user.manager is not None and (user.manager not in ids or user.manager == user.id):
                raise ValueError(f"invalid manager for {user.id}")
        if len({a.id for a in self.actions}) != len(self.actions):
            raise ValueError("action ids must be unique")
        occupied: dict[str, list[tuple[datetime, datetime]]] = {}
        for action in sorted(self.actions, key=lambda a: a.at):
            if action.id.startswith("routine-"):
                raise ValueError(
                    "action ids beginning with routine- are reserved for background support"
                )
            if action.at.tzinfo is None:
                raise ValueError(f"{action.id}: at must be timezone-aware")
            duration = timedelta(
                seconds=action.count * 2 + 600 if isinstance(action, Files) else 600
            )
            if not self.start <= action.at or action.at + duration >= self.end:
                raise ValueError(f"{action.id}: entire lifecycle must fit inside window")
            if action.user not in ids:
                raise ValueError(f"{action.id}: unknown user {action.user}")
            if isinstance(action, Call) and action.request == "mfa_reset":
                user = next(u for u in self.users if u.id == action.user)
                if action.verification == "approved" and user.manager is None:
                    raise ValueError(f"{action.id}: manager approval needs a manager")
                if action.verification == "approved" and not action.execute:
                    raise ValueError(
                        f"{action.id}: denial after approval is not a supported recovery outcome"
                    )
            if (
                isinstance(action, Call)
                and action.request == "vpn"
                and (action.verification != "approved" or not action.execute)
            ):
                raise ValueError(
                    f"{action.id}: VPN support has no recovery verification/denial options"
                )
            for start, end in occupied.get(action.user, []):
                if action.at < end and action.at + duration > start:
                    raise ValueError(f"{action.id}: overlapping lifecycles for {action.user}")
            occupied.setdefault(action.user, []).append((action.at, action.at + duration))
        return self


SOURCES = ("idp", "itsm", "telephony", "hr", "saas_files", "transcripts")


class IdpData(StrictModel):
    event_type: Literal["login", "mfa_reset", "factor_remove", "factor_enroll"]
    actor: str
    user: str
    outcome: Literal["success"] = "success"
    factor_id: str | None = None
    destination: str | None = None
    session_id: str | None = None
    device_id: str | None = None
    src_ip: str | None = None


class ItsmData(StrictModel):
    ticket_id: str
    user: str
    action: Literal["opened", "approval_requested", "approval_granted", "reset", "denied", "closed"]
    category: Literal["mfa_reset", "vpn", "migration"]
    approver: str | None = None


class TelephonyData(StrictModel):
    call_id: str
    user: str
    event_type: Literal["started", "ended"]
    from_number: str
    to_number: str
    duration_s: int | None = Field(default=None, ge=0)


class HrData(StrictModel):
    user: str
    status: Literal["notice_given"]
    last_day: datetime


class FileData(StrictModel):
    user: str
    action: Literal["download", "share_external"]
    object_id: str
    bytes: int = Field(ge=0)
    recipient: str | None = None
    ticket_id: str | None = None
    session_id: str | None = None
    device_id: str | None = None
    src_ip: str | None = None


class TranscriptData(StrictModel):
    conversation_id: str
    user: str
    channel: Literal["call", "chat"]
    speaker: Literal["caller", "agent"]
    turn_index: int = Field(ge=1)
    text: str = Field(min_length=1)
    duration_s: int = Field(default=1, ge=1)
    wait_before_s: int = Field(default=0, ge=0)
    wait_reason: Literal["manager_approval", "factor_enrollment", "client_restart"] | None = None


DATA_MODELS = dict(
    zip(SOURCES, (IdpData, ItsmData, TelephonyData, HrData, FileData, TranscriptData), strict=True)
)


class Record(StrictModel):
    record_id: str
    source: Literal["idp", "itsm", "telephony", "hr", "saas_files", "transcripts"]
    event_time: datetime
    available_at: datetime
    correlation_id: str
    data: dict

    @model_validator(mode="after")
    def check_record(self) -> "Record":
        if self.event_time.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("record times must be timezone-aware")
        if self.available_at < self.event_time:
            raise ValueError("available_at precedes event_time")
        DATA_MODELS[self.source].model_validate(self.data)
        return self


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently keeping the final value."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        try:
            unique_keys = set(keys)
        except TypeError as exc:
            raise ValueError("YAML mapping keys must be scalar values") from exc
        if len(keys) != len(unique_keys):
            raise ValueError("duplicate YAML mapping key")
        return super().construct_mapping(node, deep=deep)


def load_scenario(path: Path) -> Scenario:
    """Load YAML without permitting executable YAML tags."""
    return Scenario.model_validate(yaml.load(path.read_text(), Loader=UniqueKeyLoader))


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
