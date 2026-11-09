"""Report metadata distributions and values exclusive to a scenario class."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from pretextforge.engine import digest, json_text


def run(bundle: Path, output: Path):
    if output.exists():
        raise ValueError("output already exists")
    truth = json.loads((bundle / "GROUND_TRUTH.json").read_text())
    groups = defaultdict(list)
    for row in truth["records"]:
        groups[row["correlation_id"]].append(row)
    counts = defaultdict(lambda: defaultdict(Counter))
    cases = [
        c
        for c in truth["cases"]
        if c["kind"] == "helpdesk" and c["class"] in {"attack", "legitimate_twin"}
    ]
    for case in cases:
        rows = groups[case["correlation_id"]]
        first = min(datetime.fromisoformat(r["event_time"]) for r in rows)
        turn = next(r for r in rows if r["source"] == "transcripts")
        features = {
            "start_hour_utc": first.hour,
            "start_minute_utc": first.minute,
            "start_10minute_bin": first.minute // 10,
            "channel": turn["data"]["channel"],
            "turn_count": sum(r["source"] == "transcripts" for r in rows),
        }
        for feature, value in features.items():
            counts[feature][str(value)][case["class"]] += 1
    warnings = []
    for feature, values in counts.items():
        for value, classes in values.items():
            if sum(classes.values()) >= 3 and len(classes) == 1:
                warnings.append(
                    {
                        "feature": feature,
                        "value": value,
                        "counts": dict(classes),
                        "warning": "class-exclusive value in this authored sample",
                    }
                )
    result = {
        "cases": len(cases),
        "ground_truth_sha256": digest(bundle / "GROUND_TRUTH.json"),
        "counts": {f: {v: dict(c) for v, c in values.items()} for f, values in counts.items()},
        "warnings": warnings,
        "method": "Univariate counts, no cross-validation or statistical significance claim.",
        "scope": "Attack vs legitimate-twin helpdesk cases; routine support and files excluded.",
    }
    output.mkdir(parents=True)
    (output / "report.json").write_text(json_text(result))
    lines = [
        "# Metadata shortcut audit",
        "",
        result["scope"],
        "",
        result["method"],
        "",
        "| Field | Value | Counts |",
        "| --- | --- | --- |",
    ]
    for warning in warnings:
        lines.append(f"| {warning['feature']} | {warning['value']} | {warning['counts']} |")
    lines += [
        "",
        "Each row identifies a metadata value observed in only one case class at least "
        "three times. Inspect these values when selecting scenarios for detection testing.",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines))
    print(output / "report.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.bundle, args.output)
