"""Offline transcript review packets and accepted-cache export."""

import json
from pathlib import Path

from .rendering import atomic_json, plans_for, read_cache


def export_cache(scenario, cache: Path, output: Path) -> dict:
    """Export accepted cache entries required by a scenario."""
    import shutil

    plans = plans_for(scenario)
    _, provenance = read_cache(plans, cache)
    if output.exists():
        raise ValueError("cache export output already exists")
    output.mkdir(parents=True)
    (output / "objects").mkdir()
    index = json.loads((cache / "index.json").read_text())
    needed = {p.conversation_id: index["entries"][p.conversation_id] for p in plans}
    for key in needed.values():
        shutil.copyfile(cache / "objects" / f"{key}.json", output / "objects" / f"{key}.json")
    atomic_json(
        output / "index.json", {"config": index["config"], "entries": needed, "schema_version": 2}
    )
    return provenance["quality"]


def audit(scenario, cache: Path, output: Path) -> dict:
    plans = plans_for(scenario)
    texts, provenance = read_cache(plans, cache)
    if output.exists():
        raise ValueError("audit output already exists")
    output.mkdir(parents=True)
    report = provenance["quality"]
    index = json.loads((cache / "index.json").read_text())
    attempts = []
    for plan in plans:
        key = index["entries"][plan.conversation_id]
        entry = json.loads((cache / "objects" / f"{key}.json").read_text())
        attempts += entry["attempts"]
    report["candidate_attempts"] = len(attempts)
    report["rejected_candidates"] = sum(bool(a["errors"]) for a in attempts)
    report["accepted_constrained_source_repairs"] = sum(
        a.get("mode") == "constrained_source_repair" and not a["errors"] for a in attempts
    )
    report["cache_total_unique_requests"] = len(list((cache / "requests").glob("*.json")))
    report["styles_by_class"] = {}
    from .engine import Builder

    builder = Builder(scenario)
    _, cases = builder.build()
    classes = {c["correlation_id"]: c["class"] for c in cases}
    for plan in plans:
        group = report["styles_by_class"].setdefault(classes[plan.conversation_id], {})
        group[plan.style] = group.get(plan.style, 0) + 1
    lines = [
        "# Transcript review packet",
        "",
        "Automated checks passed; human review unrecorded.",
        "",
    ]
    for plan in plans:
        lines += [
            f"## {plan.conversation_id} ({classes[plan.conversation_id]}, {plan.channel})",
            "",
        ]
        for turn, text in zip(plan.turns, texts[plan.conversation_id], strict=True):
            lines += [f"- {turn.index} {turn.speaker} [{', '.join(turn.acts)}]: {text}"]
        lines.append("")
    (output / "transcripts.md").write_text("\n".join(lines))
    atomic_json(output / "report.json", report)
    return report
