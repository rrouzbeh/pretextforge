"""Seeded end-to-end scenario sweep; temporary event bundles are removed after checking."""

import argparse
import json
import random
import tempfile
from datetime import timedelta, timezone
from pathlib import Path

from pretextforge.bundle import generate
from pretextforge.models import Baseline, Call, Files, Scenario, load_scenario
from pretextforge.quality import evaluate


def run(args):
    if args.output.exists():
        raise ValueError("sweep output already exists")
    if args.seeds < 1:
        raise ValueError("seeds must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    base = load_scenario(Path("scenarios/acme.yaml"))
    user = base.actions[0].user
    runs = 0
    records = 0
    failures = []
    with tempfile.TemporaryDirectory(prefix="pretextforge-sweep-") as directory:
        for seed in range(args.seeds):
            rng = random.Random(seed)
            actions = []
            for i in range(2):
                verification = rng.choice(("approved", "refused", "not_attempted"))
                at = base.start + timedelta(minutes=30 + 120 * i)
                at = at.astimezone(timezone(timedelta(hours=rng.choice((-5, 0, 3)))))
                actions.append(
                    Call(
                        id=f"review-call-{i}",
                        at=at,
                        user=user,
                        verification=verification,
                        execute=verification == "approved" or rng.choice((True, False)),
                        urgent=rng.choice((True, False)),
                        channel=rng.choice(("call", "chat")),
                        pretext=rng.choice(("lost_phone", "replacement_device")),
                        malicious=rng.choice((True, False)),
                    )
                )
            actions.append(
                Files(
                    id="review-files",
                    at=base.start + timedelta(hours=5),
                    user=user,
                    count=rng.choice((1, 19, 20, 21, 101)),
                    departure=rng.choice((True, False)),
                    approved_migration=rng.choice((True, False)),
                )
            )
            rng.shuffle(actions)
            try:
                s = Scenario.model_validate(
                    {
                        **base.model_dump(),
                        "seed": seed,
                        "workdays": rng.randint(1, 5),
                        "actions": actions,
                        "observation": rng.choice(("complete", "degraded")),
                        "baseline": Baseline(
                            daily_logins_per_user=rng.randint(0, 3),
                            daily_files_per_user=rng.randint(0, 3),
                            daily_helpdesk=rng.randint(0, 3),
                        ),
                    }
                )
                path = Path(directory) / str(seed)
                q = generate(s, path)
                assert q["passed"]
                assert evaluate(path)["passed"]
                records += q["canonical_records"]
                runs += 1
            except Exception as exc:
                failures.append({"seed": seed, "error": str(exc)})
    result = {
        "passed": not failures,
        "valid_scenarios_exercised": runs,
        "canonical_records_checked": records,
        "failures": failures,
        "dimensions": [
            "seed",
            "profile",
            "timezone offset",
            "action input order",
            "call/chat",
            "recovery pretext",
            "verification/outcome",
            "intent",
            "file threshold",
            "departure/approval",
            "baseline means",
        ],
    }
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, default=80)
    run(parser.parse_args())
