"""Author a mostly benign enterprise week using the existing scenario contract."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import yaml
from build_large_scenario import build as enterprise_week

from pretextforge.models import Scenario


def build() -> Scenario:
    source = enterprise_week()
    start = datetime(2026, 11, 9, 17, tzinfo=UTC)
    shift = start - source.start
    data = source.model_dump(mode="python")
    data.update(name="northstar-benign-heavy-week", seed=20261107, start=start)
    actions = []
    # Keep varied outcomes: denied/executed resets, missing verification,
    # approved malicious collection and collection without departure notice.
    impersonations = {1, 6, 11, 16, 19}
    collections = {1, 6, 11, 16, 20}
    for index, original in enumerate(source.actions, 1):
        action = original.model_dump(mode="python")
        case_number = int(original.id.rsplit("-", 1)[1])
        if original.malicious:
            retained = case_number in (
                impersonations if original.type == "helpdesk" else collections
            )
            if not retained:
                action["malicious"] = False
                if original.type == "helpdesk":
                    action.update(true_actor=original.user, verification="approved", execute=True)
                else:
                    action.update(
                        approved_migration=True,
                        recipient=f"archive-{case_number}@partner.example",
                    )
        action.update(id=f"case-{index:03d}", at=original.at + shift)
        actions.append(action)
    data["actions"] = actions
    return Scenario.model_validate(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive creation preserves previously authored scenarios.
    with args.output.open("x") as stream:
        stream.write(yaml.safe_dump(build().model_dump(mode="json"), sort_keys=False))
    print(args.output)
