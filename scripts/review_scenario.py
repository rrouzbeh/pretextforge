"""Measure coherence and repetition independently from generation-quality reports."""

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from pretextforge.engine import json_text
from pretextforge.quality import evaluate


def review(bundle: Path) -> dict:
    quality = evaluate(bundle)
    assert quality["passed"], quality["errors"]
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    reference = json.loads((bundle / "reference.json").read_text())
    names = sorted((u["name"] for u in reference["users"]), key=len, reverse=True)
    groups = defaultdict(list)
    daily = defaultdict(Counter)
    for row in truth["records"]:
        groups[row["correlation_id"]].append(row)
        daily[row["event_time"][:10]][row["source"]] += 1

    def normalize(text):
        for name in names:
            text = text.replace(name, "PERSON")
        text = re.sub(r"\+\d[\d -]{5,}\d", "PHONE", text)
        return " ".join(re.findall(r"\w+", text.lower()))

    utterances, scripts, durations, speaking_rates, call_rates = [], [], [], [], []
    waits = Counter()
    for rows in groups.values():
        turns = [r for r in rows if r["source"] == "transcripts"]
        if not turns:
            continue
        scripts.append(tuple((r["data"]["speaker"], normalize(r["data"]["text"])) for r in turns))
        utterances += [normalize(r["data"]["text"]) for r in turns]
        words = sum(len(r["data"]["text"].split()) for r in turns)
        speech = sum(r["data"]["duration_s"] for r in turns)
        speaking_rates.append(round(words * 60 / speech, 1))
        for r in turns:
            if r["data"]["wait_reason"]:
                waits[r["data"]["wait_reason"]] += r["data"]["wait_before_s"]
        end = next(
            (r for r in rows if r["source"] == "telephony" and r["data"]["event_type"] == "ended"),
            None,
        )
        if end:
            duration = end["data"]["duration_s"]
            durations.append(duration)
            call_rates.append(round(words * 60 / duration, 1))
    bins = defaultdict(Counter)
    case_classes = Counter()
    for case in truth["cases"]:
        case_classes[case["class"]] += 1
        if case["kind"] != "helpdesk" or case["class"] == "routine":
            continue
        start = min(r["event_time"] for r in groups[case["correlation_id"]])
        bins[str(datetime.fromisoformat(start).minute // 10)][str(case["malicious"])] += 1
    summary = {
        "quality_passed": quality["passed"],
        "canonical_records": len(truth["records"]),
        "case_classes": dict(case_classes),
        "conversations": len(scripts),
        "turns": len(utterances),
        "unique_normalized_utterances": len(set(utterances)),
        "unique_normalized_conversations": len(set(scripts)),
        "duplicate_conversation_excess": len(scripts) - len(set(scripts)),
        "call_duration_seconds": {
            "min": min(durations),
            "median": statistics.median(durations),
            "max": max(durations),
            "distinct": len(set(durations)),
        }
        if durations
        else {},
        "median_speech_words_per_minute": statistics.median(speaking_rates)
        if speaking_rates
        else 0,
        "median_call_words_per_minute_including_waits": statistics.median(call_rates)
        if call_rates
        else 0,
        "explained_wait_seconds": dict(waits),
        "daily_records_by_source": dict(daily),
        "scripted_helpdesk_intent_by_10minute_bin": dict(bins),
        "most_common_normalized_utterances": Counter(utterances).most_common(12),
        "limits": (
            "Synthetic generic logs and constrained dialogue; "
            "no independent human review or calibrated real-world behavior benchmark."
        ),
    }
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = review(args.bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_text(result))
    print(json_text(result))
