"""Command-line interface for scenario validation, authoring and dataset generation."""

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from .bundle import generate
from .engine import json_text
from .models import load_scenario
from .quality import evaluate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pforge", description="Synthetic social-engineering data generation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate scenario and cross-references")
    validate.add_argument("scenario", type=Path)
    build = commands.add_parser("generate", help="generate a checked, deterministic bundle")
    build.add_argument("scenario", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--profile", choices=["complete", "degraded"])
    build.add_argument("--baseline-only", action="store_true")
    build.add_argument("--text-mode", choices=["template", "cached_llm"], default="template")
    build.add_argument("--render-cache", type=Path)
    plan = commands.add_parser("plan", help="export private typed conversation plans")
    plan.add_argument("scenario", type=Path)
    plan.add_argument("--output", type=Path, required=True)
    render = commands.add_parser("render", help="author and validate model text into a file cache")
    render.add_argument("scenario", type=Path)
    render.add_argument("--cache", type=Path, required=True)
    render.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    render.add_argument("--model", required=True)
    render.add_argument("--revision", required=True)
    render.add_argument("--attempts", type=int, default=3)
    for name, help_text in (
        ("audit", "write a transcript review packet"),
        ("cache-export", "export accepted text for offline generation"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("scenario", type=Path)
        command.add_argument("--cache", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    quality = commands.add_parser("eval", help="check an existing bundle, including file digests")
    quality.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in {"audit", "cache-export"}:
            from .auditing import audit, export_cache

            function = audit if args.command == "audit" else export_cache
            result = function(load_scenario(args.scenario), args.cache, args.output)
            print(json_text(result), end="")
            return 0
        elif args.command in {"plan", "render"}:
            from .rendering import Endpoint, ModelConfig, plans_for, render

            plans = plans_for(load_scenario(args.scenario))
            if args.command == "plan":
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x") as stream:
                    stream.write(json_text([p.model_dump() for p in plans]))
                return 0
            if not 1 <= args.attempts <= 10:
                raise ValueError("attempts must be between 1 and 10")
            config = ModelConfig(model=args.model, revision=args.revision)
            result = render(
                plans, args.cache, Endpoint(args.endpoint, config), attempts=args.attempts
            )
            print(json_text(result), end="")
            return 0 if result["passed"] else 1
        elif args.command in {"validate", "generate"}:
            scenario = load_scenario(args.scenario)
            if args.command == "validate":
                print(
                    json_text(
                        {
                            "valid": True,
                            "name": scenario.name,
                            "users": len(scenario.users),
                            "actions": len(scenario.actions),
                        }
                    ),
                    end="",
                )
                return 0
            updates = {}
            if args.profile:
                updates["observation"] = args.profile
            if args.baseline_only:
                updates["actions"] = []
            scenario = scenario.model_copy(update=updates)
            if args.text_mode == "cached_llm" and args.render_cache is None:
                raise ValueError("cached_llm mode requires --render-cache")
            if args.text_mode == "template" and args.render_cache is not None:
                raise ValueError("use --text-mode cached_llm with --render-cache")
            print(
                json_text(generate(scenario, args.output, render_cache=args.render_cache)), end=""
            )
        elif args.command == "eval":
            result = evaluate(args.bundle)
            print(json_text(result), end="")
            return 0 if result["passed"] else 1
        return 0
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        print(f"pforge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
