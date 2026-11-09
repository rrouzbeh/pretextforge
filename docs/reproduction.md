# Reproduce generation offline

From the repository root, install dependencies and run the checks:

```bash
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Generate and independently evaluate the packaged real-model example without a model
server, weights, GPU or API key:

```bash
uv run pforge generate scenarios/llm-pilot.yaml --text-mode cached_llm \
  --render-cache examples/llm-pilot-cache --output output/rebuilt-pilot-new
uv run pforge eval output/rebuilt-pilot-new
uv run pforge audit scenarios/llm-pilot.yaml --cache examples/llm-pilot-cache \
  --output output/pilot-audit-new
```

The larger template workload and seeded scenario sweep also run offline:

```bash
uv run python scripts/test_large_scenario.py \
  --scenario scenarios/benign-heavy-week.yaml --output output/generation-check-new
uv run python scripts/test_scenario_sweep.py --output output/scenario-sweep-new.json
```

The workload runner evaluates complete/degraded bundles, repeats each in a fresh
process with a different Python hash seed, compares every bundle byte, checks shared
canonical history and generates a baseline-only control. It records volume, wall time
and peak process RSS outside the deterministic generator.

For cached LLM large workloads, add `--render-cache PATH` using a cache accepted for
the selected scenario. Follow [the authoring guide](llm-workflow.md) to create one;
the [M4 instructions](local-mac.md) describe server setup, cache authoring and measured throughput.
Use fresh output paths. Existing generated bundles are never overwritten.
