"""Private turn plans and validation for offline transcript authoring."""

import hashlib
import json
import re
from collections import Counter
from typing import Literal

from .models import StrictModel

Act = Literal[
    "identity_claim",
    "sensitive_request",
    "offer_verification",
    "urgency",
    "accept_verification",
    "destination_change",
    "approval_claim",
    "bypass_request",
    "verification_missing",
    "action_commit",
    "action_hold",
    "troubleshooting",
    "closing",
]


class TurnPlan(StrictModel):
    record_id: str
    index: int
    speaker: Literal["caller", "agent"]
    event_time: str | None = None
    stage: str = ""
    acts: list[Act]
    template_text: str
    required_literals: list[str]


class ConversationPlan(StrictModel):
    conversation_id: str
    user: str
    channel: Literal["call", "chat"]
    style: str
    turns: list[TurnPlan]


def semantic_plan(plan: ConversationPlan) -> dict:
    """Cache wording against facts/order; speech clocks are derived from accepted words."""
    result = plan.model_dump()
    for turn in result["turns"]:
        turn["event_time"] = None
    return result


def content_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


# Acceptance vocabulary for planned behaviors. Reject unsupported wording so that
# rendering cannot change the scenario's actions or labels.
ACT_PATTERNS = {
    "identity_claim": r"\b(?:this is|i am|i'm|my name)\b",
    "sensitive_request": r"\b(?:mfa|multi.factor|authentication)\b.*\b(?:reset|recover)\w*\b|"
    r"\b(?:reset|recover)\w*\b.*\b(?:mfa|multi.factor|authentication)\b",
    "offer_verification": r"\b(?:manager|supervisor)\b.*"
    r"\b(?:approval|approv|verify|verification)\w*\b|"
    r"\b(?:approval|verify|verification)\b.*\b(?:manager|supervisor)\b",
    "urgency": r"\b(?:urgent|urgently|hurry|boarding|soon|rush|quickly|immediately)\b",
    "accept_verification": r"\b(?:contact|check with|ask|call|reach|approv)\w*\b.*"
    r"\b(?:manager|supervisor)\b",
    "destination_change": r"\b(?:number|replacement|factor|phone)\b",
    "approval_claim": r"\b(?:approval|approved)\b.*\b(?:recorded|received|confirmed|logged)\b",
    "bypass_request": r"\b(?:skip|bypass|without|don't contact|do not contact|avoid|forego)\b.*"
    r"\b(?:manager|approval|verification)\b",
    "verification_missing": r"\b(?:not|no|hasn't|haven't|missing|pending|unrecorded)\b.*"
    r"\b(?:approval|recorded|approved)\b|"
    r"\bapproval\b.*\b(?:not|missing|pending|hasn't)\b",
    "action_commit": r"\b(?:will|going to|i'll)\b.*\breset\b",
    "action_hold": r"\b(?:hold|wait|pending|cannot|can't|won't|will not)\b",
    "troubleshooting": r"\bvpn\b|\b(?:connect|restart)\w*\b",
    "closing": r"\b(?:close|closing|thank|resolved|connects|working)\w*\b",
}


def act_present(act: str, text: str) -> bool:
    found = bool(re.search(ACT_PATTERNS[act], text, re.I))
    if act == "approval_claim":
        found = found or bool(
            re.search(
                r"\b(?:manager|supervisor)\b[^.!?]*\b(?:has |had |already )?approved\b|"
                r"\bapproval\b[^.!?]*\bgranted\b",
                text,
                re.I,
            )
        )
        if re.search(r"\b(?:if|once|until|when|after|before)\b[^.!?]*\bapprov\w*\b", text, re.I):
            return False
    if act == "approval_claim" and re.search(r"\b(?:not|no|hasn't|haven't|pending)\b", text, re.I):
        return False
    if act == "action_commit" and re.search(
        r"\b(?:hold|pending|cannot|can't|won't|will not|not reset)\b", text, re.I
    ):
        return False
    return found


def validate_turn(plan: TurnPlan, text: str) -> list[str]:
    errors = []
    if not text.strip() or len(text) > 900 or "\n" in text:
        errors.append("expected one nonempty utterance under 900 characters")
    if re.match(r"\s*(?:caller|agent|assistant|user)\s*:", text, re.I):
        errors.append("speaker label included in utterance")
    if text.lstrip().startswith("{") or re.search(r'"(?:speaker|utterance)"\s*:', text):
        errors.append("serialized metadata in utterance")
    if len(text.split()) > len(plan.template_text.split()) * 2.5 + 6:
        errors.append("utterance adds too much unplanned material")
    if plan.stage == "mfa_open_lost_phone" and not re.search(
        r"\b(lost|missing|misplaced|left behind|cannot find|can't find)\b", text, re.I
    ):
        errors.append("lost-phone fact omitted")
    if plan.stage == "mfa_open_replacement_device" and not re.search(
        r"\b(replace\w*|new|changed|failed|stopped)\b", text, re.I
    ):
        errors.append("replacement-device fact omitted")
    if plan.stage == "execute" and not re.search(
        r"\byou\b.*\b(?:enroll|register|add)\b", text, re.I
    ):
        errors.append("caller enrollment instruction omitted or actor changed")
    if plan.stage == "recovered" and not re.search(
        r"\b(?:sign|log\w*|access|account|factor|reach)\b", text, re.I
    ):
        errors.append("restored-access fact omitted")
    if plan.stage == "vpn_fixed" and not re.search(
        r"\b(?:connect\w*|working|resolved|restored)\b", text, re.I
    ):
        errors.append("restored-VPN fact omitted")
    if plan.stage in {"recovered", "vpn_fixed"} and re.search(
        r"\b(?:not|cannot|can['’]t|unable|failed|failing|broken|unavailable|error)\b", text, re.I
    ):
        errors.append("successful recovery contradicted")
    if plan.stage in {"ask_verification", "request_approval"} and not re.search(
        r"\b(?:i|me|we)\b", text, re.I
    ):
        errors.append("agent approval-request role changed")
    if plan.stage == "close" and not re.search(r"\bclos\w*\b", text, re.I):
        errors.append("ticket-closure statement omitted")
    if plan.stage == "urgency" and re.search(r"\bas soon as i have\b", text, re.I):
        errors.append("deadline meaning changed")
    if plan.stage == "mfa_open_lost_phone" and re.search(
        r"\breset\b.*\bon\b.*\b(?:lost|missing)\b.*\bphone\b", text, re.I
    ):
        errors.append("recovery request changed to resetting missing handset")
    if plan.speaker == "caller" and re.search(r"\byour manager\b", text, re.I):
        errors.append("caller refers to agent manager instead of own manager")
    if plan.speaker == "agent" and re.search(r"\b(?:my|our) (?:manager|supervisor)\b", text, re.I):
        errors.append("agent refers to own manager instead of caller manager")
    if re.search(r"\bfactor(?: id)?\s+is\s+\+\d", text, re.I):
        errors.append("phone destination described as factor identifier")
    for literal in plan.required_literals:
        if f"[{literal}]" in text:
            errors.append("bracketed literal token")
        if literal not in text:
            errors.append(f"required literal absent: {literal}")
    for act in plan.acts:
        if not act_present(act, text):
            errors.append(f"planned behavior missing: {act}")
    for act in ("bypass_request", "approval_claim", "action_commit"):
        if act not in plan.acts and act_present(act, text):
            errors.append(f"unplanned sensitive behavior: {act}")
    if "action_commit" in plan.acts and re.search(
        r"\b(?:won't|will not|cannot|can't|not reset)\b", text, re.I
    ):
        errors.append("commitment negated")
    if "approval_claim" in plan.acts and re.search(r"\b(?:not|no|hasn't|haven't)\b", text, re.I):
        errors.append("approval claim negated")
    if "action_hold" in plan.acts and re.search(
        r"\b(?:already reset|have reset|reset is complete)\b", text, re.I
    ):
        errors.append("denied action asserted complete")
    if (
        "offer_verification" not in plan.acts
        and re.search(r"\b(?:will|while i|let me|going to)\b.*\b(?:request|ask|seek)\b", text, re.I)
        and re.search(r"\b(?:approval|manager|supervisor|verification)\b", text, re.I)
    ):
        errors.append("unplanned future verification request")
    allowed_numbers = set(re.findall(r"\+?[\d][\d -]{5,}[\d]", plan.template_text))
    actual_numbers = set(re.findall(r"\+?[\d][\d -]{5,}[\d]", text))
    if actual_numbers - allowed_numbers:
        errors.append("invented phone/number")
    if re.search(r"\b(?:GROUND_TRUTH|true_actor|malicious|case_id)\b", text):
        errors.append("private metadata marker")
    return errors


def quality_summary(plans: list[ConversationPlan], texts: dict[str, list[str]]) -> dict:
    phrases = Counter()
    failures = []
    unchanged = 0
    for plan in plans:
        rendered = texts.get(plan.conversation_id, [])
        if not isinstance(rendered, list) or len(rendered) != len(plan.turns):
            failures.append({"conversation_id": plan.conversation_id, "error": "turn count"})
            continue
        for turn, text in zip(plan.turns, rendered, strict=True):
            if not isinstance(text, str):
                failures.append({"record_id": turn.record_id, "error": "utterance is not a string"})
                continue
            unchanged += text == turn.template_text
            for error in validate_turn(turn, text):
                failures.append({"record_id": turn.record_id, "error": error})
            phrases.update(
                set(
                    " ".join(re.findall(r"\w+", text.lower())[i : i + 5])
                    for i in range(max(0, len(re.findall(r"\w+", text)) - 4))
                )
            )
    return {
        "conversations": len(plans),
        "turns": sum(len(p.turns) for p in plans),
        "unchanged_template_turns": unchanged,
        "changed_turns": sum(len(p.turns) for p in plans) - unchanged,
        "passed": not failures,
        "failures": failures,
        "repeated_5grams": sorted(phrases.items(), key=lambda pair: (-pair[1], pair[0]))[:20],
        "semantic_limit": "Conservative phrase checks; not proof of semantic equivalence.",
        "human_review": "not performed unless separately recorded",
    }
