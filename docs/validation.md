# Validation record

This record covers dataset consistency, reproducibility, text-authoring statistics
and the commands used to verify them.

## Recorded workload

The workload run on **2026-11-07** used
[`benign-heavy-week.yaml`](../scenarios/benign-heavy-week.yaml): 500 users, 10
departments and five workdays, with 100 scripted cases and seeded routine work.
The scenario's SHA-256 digest is recorded in [validation-results.json](validation-results.json).

The run used accepted Qwen3.5-4B Q4_K_M text, authored separately on an Apple M4
Mac mini with 16 GB memory. Both observation profiles passed independent evaluation
and reproduced byte-for-byte in fresh processes using different Python hash seeds.
Their canonical histories matched. The baseline-only control passed validation and
contained zero malicious cases.

| Measurement | Complete | Degraded |
| --- | ---: | ---: |
| Canonical records | 122,524 | 122,524 |
| Observed records | 122,524 | 122,014 |
| Dropped records | 0 | 510 |
| Generation time | 8.778 s | 8.649 s |
| Peak process RSS | 1,029.1 MiB | 1,024.8 MiB |

Timing and memory are single-run observations that exclude model authoring and
model-server memory. The test runner accounts for the
different RSS units reported by macOS and Linux.

The accompanying seeded sweep exercised **80 valid scenario variations and 23,056
canonical records**, with no failures. Dimensions included input ordering, timezone
offsets, collection profiles, verification, execution, intent, file volume and
baseline workloads.

The checked-in JSON is a compact extract of those completed local reports, including
the source report digests and model configuration. Large evidence bundles and the
full 500-user render cache are excluded from Git. The packaged pilot cache is included
so that the cached-text path can be exercised from a fresh checkout.

## Text quality

The 500-user cache contains 326 conversations and 1,622 turns. Of those turns, 893
differ from their templates and 729 are unchanged; 91 used a recorded exact-source
repair. These are expanded turn counts, so reused wording contributes multiple times.

Automated checks cover literal values, supported actions, speaker constraints and
timing. The audit packet contains accepted text and planned behaviors for manual
inspection; the recorded counts do not represent an independent human review.

## Reproduce the checks

From the repository root:

```bash
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python scripts/test_scenario_sweep.py --output output/sweep-check.json
uv run python scripts/test_large_scenario.py \
  --scenario scenarios/benign-heavy-week.yaml --output output/workload-check
```

The workload command above uses template wording. To repeat the cached-text workload,
first author a cache for the same scenario, then add `--render-cache PATH`. New model
inference can produce different accepted text and timings; exact byte reproduction
requires the same accepted cache. See [the LLM workflow](llm-workflow.md).

The packaged pilot can be generated entirely offline after dependency installation:

```bash
uv run pforge generate scenarios/llm-pilot.yaml --text-mode cached_llm \
  --render-cache examples/llm-pilot-cache --output output/pilot-check
uv run pforge eval output/pilot-check
```

Always use new output paths. `scripts/test_large_scenario.py` retains its generated
bundles and reports; the scenario sweep uses temporary bundles and retains its summary.

## Repository verification

On **2026-11-09**, the repository passed Ruff lint/format checks and all **87 tests**. The suite includes tests
for corrupt evidence, source causality, active factors and sessions, cache validation,
offline cache export and the removal of detection commands.

The README quick start also passed from a clean temporary copy containing only
files selected for version control: dependency installation, scenario validation,
complete/degraded generation and evaluation, packaged cached-text generation and
transcript audit. No local model weights or ignored experiment caches were copied.
