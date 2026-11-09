# Northstar: a mostly benign enterprise week

This fictional workload has 500 employees across 10 departments over November
9–13, 2026. Workdays start at 17:00 UTC (09:00 Vancouver standard time). The scenario combines identity recovery and file activity. Its input is [benign-heavy-week.yaml](benign-heavy-week.yaml).

## Activity mix

| Scripted activity | Benign | Malicious |
| --- | ---: | ---: |
| Verified lost-phone recovery | 35 | 0 |
| Real employees with incomplete verification | 20 | 0 |
| MFA impersonation | 0 | 5 |
| Approved file migration | 35 | 0 |
| Insider file collection/external sharing | 0 | 5 |
| Total | 90 | 10 |

Routine workload means are four logins and 36 downloads per employee per day,
plus 50 support calls/chats per day. Seeded variation produces 266 routine support
conversations in addition to the 60 scripted recovery conversations. The other
40 scripted cases concern files. Routine activity is benign. The 10% malicious
share applies only to the 100 scripted cases, not all conversations or log records.
The case mix and activity rates are configured in the scenario.

Both legitimate and malicious activity include urgency, unfamiliar phone numbers,
different verification outcomes and file volumes around the threshold. Two malicious
file cases have migration approval and one lacks departure notice. The private answer key records intent separately from those observable events.

## Generate and inspect

```bash
uv run pforge validate scenarios/benign-heavy-week.yaml
uv run python scripts/test_large_scenario.py \
  --scenario scenarios/benign-heavy-week.yaml --output output/benign-heavy-new
```

Choose a fresh output directory. The runner checks complete/degraded profiles,
independent evaluation, byte-identical fresh-process repeats, matching canonical
histories and a baseline-only control.

The supplied seed produces 122,524 complete records and 122,014 observed degraded
records (510 dropped). See [recorded validation results](../docs/validation.md). Start with a bundle's
`GROUND_TRUTH.md` to follow the stories, then inspect `logs/` and the quality reports.
Private truth is kept separate from public evidence.

For local model setup and cached LLM output, see [the M4 workflow](../docs/local-mac.md).
Recreate the YAML without overwriting it with:

```bash
uv run python scripts/build_benign_heavy_scenario.py --output /tmp/northstar-new.yaml
```

The authoring helper derives a fixed variant of the existing enterprise scenario,
configures approved recoveries and migrations for the benign cases, and validates
the resulting scenario.
