# Northstar enterprise week

A fictional 500-person company operates across finance, engineering, support,
sales, operations, people, legal, marketing, security and research. Each department
has 50 named employees and a manager of record. The shared service desk is modeled
as one logical agent/queue. All employees and activity are synthetic.

## The week

Monday through Friday, 09:00 UTC onward, workload means are four authentications
and thirty-six routine downloads per employee per day. Counts vary by user/day.
About fifty VPN-support calls/chats occur each morning,
then twelve scripted recovery conversations in the afternoon. Their office slots are
shuffled independently of malicious intent; an account’s authorized recovery may
precede or follow its impersonation. Scheduled support
conversations are spaced to avoid overlapping the modeled desk's calls.

Each day has four pairs of overlapping file campaigns, involving separate users.
The file activity and routine work intentionally overlap. Counts vary between 25 and
2,000 downloads, with cases at 99, 100 and 101 around the policy threshold of 100.

## Scripted cases

| Family | Cases | What it tests |
| --- | ---: | --- |
| Impersonation | 20 | Calls and chats, explicit bypass vs. missing verification, 15 executed and 5 denied requests |
| Verified recovery | 20 | The same accounts also recover legitimately; manager identity and factor replacement must stay consistent |
| Unverified real employee | 20 | Legitimate intent with missing procedure, including executed resets; procedure and intent are separate annotations |
| Malicious file collection | 20 | Small and large bursts, external shares, six cases with absent departure context or valid migration approval |
| Approved migration | 20 | Matched file volumes with real authorization, including users under departure notice |

There are 40 malicious and 60 legitimate scripted cases, plus a seeded variable number of routine support
conversations. The two malicious actors with migration approval and the four without
departure notices provide contrasting approval and HR contexts. Two denied
impersonations also lack an explicit bypass request and have no executed reset.
Their private intent remains separate from public evidence.

## Run and test

```bash
uv run pforge validate scenarios/enterprise-week.yaml
uv run python scripts/test_large_scenario.py --output output/enterprise-test
```

The test runner generates complete/degraded bundles in separate processes, evaluates
them independently, generates both again with different Python hash seeds, compares
every file digest, checks matching canonical history and runs a baseline-only control.
It saves the generated bundles, quality reports, a Markdown summary and JSON
timing/memory measurements.

Use a new output directory for a repeat run. The YAML is standalone and can be edited
directly. `scripts/build_large_scenario.py --output another-path.yaml` reproduces the
authored YAML; it is a convenience script, not an added generator requirement.

## Output checks

Quality checks cover schema, chronology, source correlation and reproducibility.
Private intent is stored in the answer key. Observation profiles change collection
while preserving canonical history. The runner's summary includes record counts,
validation results, generation time and peak process memory.
