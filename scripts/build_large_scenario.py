"""Author a fixed, reviewable YAML workload; not part of dataset generation."""

import argparse
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from pretextforge.engine import stable_seed
from pretextforge.models import Scenario, timestamp

DEPARTMENTS = (
    "finance",
    "engineering",
    "support",
    "sales",
    "operations",
    "people",
    "legal",
    "marketing",
    "security",
    "research",
)
FIRST_NAMES = """
Alex Avery Blake Cameron Casey Charlie Dakota Dana Devon Drew
Eden Ellis Emery Finley Frankie Harper Hayden Jamie Jesse Jordan
Jules Kai Kendall Lane Lee Logan Marley Micah Morgan Noel
Oakley Parker Pat Peyton Quinn Reese Remy Riley Robin Rory
Rowan Sage Sam Sasha Shawn Sidney Sky Taylor Terry Toni
""".split()
LAST_NAMES = "Chen Ortiz Brooks Kim Reed Park Bell Fox Hill Wong".split()


def build() -> Scenario:
    start = datetime(2026, 3, 2, 9, tzinfo=UTC)
    users = []
    for department, surname in zip(DEPARTMENTS, LAST_NAMES, strict=True):
        manager = f"alex.{surname.lower()}"
        for first in FIRST_NAMES:
            index = len(users)
            user_id = f"{first.lower()}.{surname.lower()}"
            users.append(
                {
                    "id": user_id,
                    "name": f"{first} {surname}",
                    "phone": f"+1-{202 + index // 100}-555-{100 + index % 100:04d}",
                    "department": department,
                    "manager": (
                        "alex.chen"
                        if index and user_id == manager
                        else manager
                        if user_id != manager
                        else None
                    ),
                }
            )
    actions = []
    counts = [25, 99, 100, 101, 250, 900, 1200, 2000, 40, 500]
    for i in range(20):
        day, slot = divmod(i, 4)
        at = start + timedelta(days=day, hours=5, minutes=slot * 30)
        user = users[(i % 10) * 50 + 1 + i // 10]["id"]
        # Both affect the same account; scheduling below is independent of intent.
        for offset, label, malicious, verification, execute, identity in (
            (
                0,
                "impersonation",
                True,
                "not_attempted" if i % 3 == 2 else "refused",
                i % 4 != 1,
                user,
            ),
            (10, "verified-recovery", False, "approved", True, user),
            (
                20,
                "unverified-employee",
                False,
                "refused" if i % 2 == 0 else "not_attempted",
                i % 2 == 0,
                users[(i % 10) * 50 + 15 + i // 10]["id"],
            ),
        ):
            actions.append(
                {
                    "type": "helpdesk",
                    "id": f"{label}-{i + 1:02d}",
                    "at": timestamp(at + timedelta(minutes=offset)),
                    "user": identity,
                    "malicious": malicious,
                    "true_actor": f"outsider-{i % 4 + 1}" if malicious else identity,
                    "channel": "chat" if i % 2 else "call",
                    "request": "mfa_reset",
                    "verification": verification,
                    "execute": execute,
                    "urgent": i % 4 != 3,
                    "caller_number": f"+1-207-555-{100 + i * 3 + offset // 10:04d}",
                    "new_number": f"+1-208-555-{100 + i * 3 + offset // 10:04d}",
                }
            )
        # Paired bursts overlap across different accounts and departments.
        file_at = start + timedelta(days=day, hours=1, minutes=slot * 15)
        for twin in (False, True):
            actions.append(
                {
                    "type": "files",
                    "id": f"{'migration' if twin else 'collection'}-{i + 1:02d}",
                    "at": timestamp(file_at),
                    "user": users[(i % 10) * 50 + 25 + 2 * (i // 10) + int(twin)]["id"],
                    "malicious": not twin,
                    "departure": i % 5 != 4,
                    # Two malicious actors have valid approval: intent stays unobservable.
                    "approved_migration": twin or i % 10 == 0,
                    "count": counts[i % len(counts)],
                    "recipient": (
                        f"archive-{i}@partner.example" if twin else f"personal-{i}@mail.example"
                    ),
                }
            )
    # Uniformly shuffle identities/case types over the same office-hours slots.
    # No label or verification outcome is consulted when assigning a time.
    for day in range(5):
        calls = [
            a
            for a in actions
            if a["type"] == "helpdesk" and datetime.fromisoformat(a["at"]).day == start.day + day
        ]
        rng = random.Random(stable_seed(f"20261106:schedule:{day}"))
        rng.shuffle(calls)
        for slot, call in enumerate(calls):
            call["at"] = timestamp(
                start
                + timedelta(days=day, hours=4, minutes=40 + slot * 12, seconds=rng.randrange(0, 90))
            )
    return Scenario.model_validate(
        {
            "name": "northstar-enterprise-week",
            "seed": 20261106,
            "start": timestamp(start),
            "workdays": 5,
            "users": users,
            "helpdesk_agent": "service.desk",
            "policy": {"bulk_download_threshold": 100},
            "baseline": {
                "daily_logins_per_user": 4,
                "daily_files_per_user": 36,
                "daily_helpdesk": 50,
            },
            "actions": actions,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("scenarios/enterprise-week.yaml"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose another path")
    args.output.write_text(yaml.safe_dump(build().model_dump(mode="json"), sort_keys=False))
    print(args.output)
