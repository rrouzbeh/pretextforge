"""Publish complete, validated bundles without overwriting existing work."""

import shutil
import tempfile
from pathlib import Path

from . import __version__
from .engine import Builder, digest, json_text, jsonl, observe, public_reference, write_logs
from .models import Scenario
from .quality import evaluate


def generate(scenario: Scenario, output: Path, *, render_cache: Path | None = None) -> dict:
    # Pydantic model_copy deliberately skips validation; callers use it for profile
    # sweeps, so validate again before trusting action combinations or publishing.
    scenario = Scenario.model_validate(scenario.model_dump())
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"output must be absent or empty: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pforge-", dir=output.parent))
    try:
        builder = Builder(scenario)
        records, cases = builder.build()
        rendering = None
        if render_cache is not None:
            from .rendering import read_cache

            texts, rendering = read_cache(builder.plans, render_cache)
            builder = Builder(scenario, transcripts=texts)
            records, cases = builder.build()
            (stage / "TURN_PLANS.json").write_text(
                json_text([p.model_dump() for p in builder.plans])
            )
        visible, observation = observe(records, scenario)
        write_logs(stage / "logs", visible)
        (stage / "reference.json").write_text(json_text(public_reference(scenario)))
        (stage / "resolved_scenario.json").write_text(json_text(scenario.model_dump(mode="json")))
        (stage / "GROUND_TRUTH.json").write_text(json_text({"cases": cases, "records": records}))
        lines = [
            f"# {scenario.name}: private answer key",
            "",
            "Synthetic evidence; not production-performance data.",
            "",
        ]
        if not any(c["malicious"] for c in cases):
            lines += ["No attack was generated.", ""]
        for case in cases:
            lines += [
                f"- {case['case_id']}: {case['class']}; user={case['user']}; "
                f"procedure_satisfied={case['procedure_satisfied']}; "
                f"correlation_id={case['correlation_id']}; action={case['action_point']}"
            ]
        (stage / "GROUND_TRUTH.md").write_text("\n".join(lines) + "\n")
        (stage / "labels").mkdir()
        (stage / "labels/cases.jsonl").write_text(jsonl(cases))
        (stage / "OBSERVATION_MANIFEST.json").write_text(json_text(observation))
        files = {
            p.relative_to(stage).as_posix(): digest(p)
            for p in sorted(stage.rglob("*"))
            if p.is_file()
        }
        manifest = {
            "engine_version": __version__,
            "scenario_seed": scenario.seed,
            "template_sha256": builder.template_digest,
            "files": files,
        }
        if rendering:
            manifest["rendering"] = rendering
        (stage / "MANIFEST.json").write_text(json_text(manifest))
        report = evaluate(stage)
        if not report["passed"]:
            raise ValueError(
                "generated bundle failed quality checks: " + "; ".join(report["errors"])
            )
        (stage / "quality_report.json").write_text(json_text(report))
        (stage / "quality_report.md").write_text(
            "# Quality report\n\nPassed all checks.\n\n"
            f"Canonical records: {report['canonical_records']}; "
            f"observed: {report['observed_records']}; dropped: {report['dropped_records']}.\n\n"
            f"Checks: {', '.join(report['checks'])}.\n"
        )
        if output.exists():
            output.rmdir()  # Will fail safely if another process populated it.
        stage.rename(output)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)
