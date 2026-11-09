"""Check enterprise generation, quality and reproducibility in fresh processes."""

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pretextforge.bundle import generate
from pretextforge.engine import digest, json_text
from pretextforge.models import load_scenario
from pretextforge.quality import evaluate


def worker(args):
    start = time.perf_counter()
    if args.worker == "generate":
        scenario = load_scenario(args.scenario).model_copy(update={"observation": args.profile})
        if args.baseline_only:
            scenario = scenario.model_copy(update={"actions": []})
        result = generate(scenario, args.output, render_cache=args.render_cache)
    elif args.worker == "eval":
        result = evaluate(args.output)
        assert result["passed"], result
    print(
        json_text(
            {
                "seconds": round(time.perf_counter() - start, 3),
                "peak_rss_mib": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    / (1024**2 if sys.platform == "darwin" else 1024),
                    1,
                ),
                "result": result,
            }
        ),
        end="",
    )


def run(args):
    if args.output.exists():
        raise ValueError("test output already exists; choose a new directory")
    args.output.mkdir(parents=True)
    scenario = load_scenario(args.scenario)
    summary = {
        "scenario": str(args.scenario),
        "scenario_sha256": digest(args.scenario),
        "users": len(scenario.users),
        "departments": len({u.department for u in scenario.users}),
        "workdays": scenario.workdays,
        "scripted_cases": len(scenario.actions),
        "profiles": {},
        "notes": [
            "Synthetic operational workload, not real company data.",
            "Runtime/RSS are single-run observations on this machine.",
            "Attack intent is deliberately unobservable in some approved/no-notice cases.",
            "Reports describe data quality and volume; they do not make detection decisions.",
        ],
    }

    def launch(operation, profile, output, *, hash_seed="1", baseline=False):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            operation,
            "--scenario",
            str(args.scenario),
            "--profile",
            profile,
            "--output",
            str(output),
        ]
        if baseline:
            command.append("--baseline-only")
        if args.render_cache:
            command += ["--render-cache", str(args.render_cache)]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            check=True,
        )
        return json.loads(completed.stdout)

    for profile in ("complete", "degraded"):
        print(f"Generating {profile}...", flush=True)
        bundle = args.output / profile
        generated = launch("generate", profile, bundle)
        assert generated["result"]["canonical_records"] >= 100_000
        checked = launch("eval", profile, bundle)
        print(f"Checking {profile} reproducibility in another process...", flush=True)
        with tempfile.TemporaryDirectory(prefix="pforge-repeat-") as temporary:
            repeat = Path(temporary) / "repeat"
            launch("generate", profile, repeat, hash_seed="999")
            hashes = {
                p.relative_to(bundle).as_posix(): digest(p)
                for p in bundle.rglob("*")
                if p.is_file()
            }
            repeated = {
                p.relative_to(repeat).as_posix(): digest(p)
                for p in repeat.rglob("*")
                if p.is_file()
            }
            assert hashes == repeated, f"{profile}: byte determinism failed"
        if profile == "degraded":
            assert generated["result"]["dropped_records"] > 0
        summary["profiles"][profile] = {
            "generate": generated,
            "eval": checked,
            "byte_identical_repeat": True,
        }
        (args.output / "summary.json").write_text(json_text(summary))
    # The observation sweep must change visibility, not the hidden historical events.
    assert digest(args.output / "complete/GROUND_TRUTH.json") == digest(
        args.output / "degraded/GROUND_TRUTH.json"
    )
    print("Generating baseline-only negative control...", flush=True)
    baseline = args.output / "baseline"
    control = launch("generate", "complete", baseline, baseline=True)
    checked_control = launch("eval", "complete", baseline)
    assert control["result"]["attacks"] == 0
    summary["baseline_control"] = {"generate": control, "eval": checked_control}
    summary["canonical_history_matches_profiles"] = True
    summary["passed"] = True
    (args.output / "summary.json").write_text(json_text(summary))
    lines = [
        "# Enterprise scenario test",
        "",
        "All workload checks passed.",
        "",
        f"{summary['users']} users; {summary['departments']} departments; "
        f"{summary['workdays']} workdays; {summary['scripted_cases']} scripted cases.",
        "",
        "| Profile | Observed records | Canonical records | Dropped | Quality passed |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for profile, item in summary["profiles"].items():
        quality = item["generate"]["result"]
        lines += [
            f"| {profile} | {quality['observed_records']} | {quality['canonical_records']} | "
            f"{quality['dropped_records']} | {item['eval']['result']['passed']} |"
        ]
    lines += [
        "",
        "Both profiles reproduce byte-for-byte in fresh processes with different "
        "Python hash seeds. Their private canonical histories match. The baseline-only "
        "control contains no malicious cases and passes independent quality validation.",
        "",
        *summary["notes"],
        "",
        "See summary.json for runtime, memory and quality checks. "
        "Each bundle retains its complete private answer key and correlated public logs.",
        "",
    ]
    (args.output / "summary.md").write_text("\n".join(lines))
    print(args.output / "summary.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=Path("scenarios/enterprise-week.yaml"))
    parser.add_argument("--output", type=Path, default=Path("output/enterprise-test"))
    parser.add_argument("--worker", choices=["generate", "eval"])
    parser.add_argument("--profile", default="complete")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--render-cache", type=Path)
    arguments = parser.parse_args()
    if arguments.worker:
        worker(arguments)
    else:
        run(arguments)
