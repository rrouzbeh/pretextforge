"""Regressions exposed by the enterprise scenario, kept small for ordinary CI."""

from pathlib import Path

import pytest

from pretextforge.engine import Builder
from pretextforge.models import Baseline, Files, load_scenario

ROOT = Path(__file__).resolve().parents[1]


def quiet_files(*, count, threshold=100):
    scenario = load_scenario(ROOT / "scenarios/acme.yaml")
    action = Files(
        id="collection", user="sam.brooks", at=scenario.actions[0].at, malicious=True, count=count
    )
    return scenario.model_copy(
        update={
            "actions": [action],
            "policy": scenario.policy.model_copy(update={"bulk_download_threshold": threshold}),
            "baseline": Baseline(daily_files_per_user=0, daily_logins_per_user=0, daily_helpdesk=0),
        }
    )


def test_enterprise_scenario_has_meaningful_coverage():
    scenario = load_scenario(ROOT / "scenarios/enterprise-week.yaml")
    assert len(scenario.users) == 500
    assert len({u.department for u in scenario.users}) == 10
    assert len(scenario.actions) == 100
    assert sum(a.malicious for a in scenario.actions) == 40
    calls = [a for a in scenario.actions if a.type == "helpdesk"]
    files = [a for a in scenario.actions if a.type == "files"]
    assert {a.channel for a in calls} == {"call", "chat"}
    assert {a.verification for a in calls} == {"approved", "refused", "not_attempted"}
    assert any(a.malicious and not a.execute for a in calls)
    assert any(not a.malicious and a.execute and a.verification != "approved" for a in calls)
    assert any(a.malicious and a.approved_migration for a in files)
    assert any(a.malicious and not a.departure for a in files)
    assert {99, 100, 101, 2000} <= {a.count for a in files}


@pytest.mark.parametrize("prior_downloads", [0, 1, 2])
def test_first_file_evidence_includes_baseline(prior_downloads):
    from datetime import timedelta

    scenario = quiet_files(count=99)
    builder = Builder(scenario)
    for i in range(prior_downloads):
        builder.add(
            f"background-{i}",
            "saas_files",
            scenario.actions[0].at - timedelta(seconds=60),
            user="sam.brooks",
            action="download",
            object_id=f"routine-{i}",
            bytes=1024,
        )
    records, cases = builder.build()
    evidence = next(r for r in records if r["record_id"] == cases[0]["first_evidence_record_id"])
    campaign = [
        r
        for r in records
        if r["correlation_id"] == cases[0]["correlation_id"] and r["source"] == "saas_files"
    ]
    if prior_downloads == 0:
        assert evidence["data"]["action"] == "share_external"
    else:
        assert evidence == campaign[100 - prior_downloads - 1]
