# Cached transcript authoring

PretextForge separates model authoring from deterministic dataset generation.
`render` prepares and validates conversation text; `generate` reads accepted entries
offline. Template mode remains available without a model runtime.

## Run the packaged example

From the repository root:

```bash
uv sync --locked --group dev
uv run pforge generate scenarios/llm-pilot.yaml --text-mode cached_llm \
  --render-cache examples/llm-pilot-cache --output output/offline-pilot
uv run pforge eval output/offline-pilot
```

The included cache contains accepted Qwen2.5-1.5B-Instruct Q4_K_M output, with model
revision and prompt settings recorded in its index. It can be reused across collection
profiles because loss and delay are applied after canonical generation.

## Render new conversations

Use an OpenAI-compatible endpoint with a base URL ending in `/v1` and support for
`response_format: {type: "json_object", schema: ...}`. The [Apple Silicon guide](local-mac.md)
provides a tested server configuration. Record the model identity and revision served
by the endpoint; use a new cache when changing models or settings.

```bash
uv run pforge plan scenarios/llm-pilot.yaml --output output/pilot-plans.json
uv run pforge render scenarios/llm-pilot.yaml --cache render_cache/pilot \
  --endpoint http://127.0.0.1:8080/v1 --model qwen3.5-4b-q4_k_m \
  --revision e87f176479d0855a907a41277aca2f8ee7a09523
uv run pforge audit scenarios/llm-pilot.yaml --cache render_cache/pilot \
  --output output/pilot-audit
uv run pforge generate scenarios/llm-pilot.yaml --text-mode cached_llm \
  --render-cache render_cache/pilot --output output/pilot-llm
uv run pforge eval output/pilot-llm
```

Wait for the endpoint's `/v1/models` readiness check before rendering. For authenticated
endpoints, supply `PFORGE_MODEL_API_KEY` through the environment. Credentials are sent
in the Authorization header and excluded from cache metadata and reports. Remote
requests use configured proxy/TLS settings; loopback requests remain local. Use one
cache-writing process at a time.

## Authoring and validation

Action builders create private turn plans with speakers, required literals, behavior
and timing constraints. The model receives batches of source utterances with names
and phone numbers replaced by slot tokens. Private intent and true actors are excluded.
Restored utterances are checked against their plans before acceptance.

Checks cover required literals, planned actions, speaker perspective, approval wording,
negation, invented numbers and private markers. For example, an agent cannot change
"your manager" to "my manager." Transcript audits pair accepted wording with planned
behaviors for manual inspection.

The default retry strategy first requests paraphrases. Its final attempt requests the
source line verbatim and records the result as `constrained_source_repair`. Audit
reports distinguish changed turns, unchanged turns and repairs. Raw candidates,
validation errors, seeds and request/response content remain in the authoring cache.
These phrase-based checks supplement transcript review.

## Cache and timing contracts

- Accepted conversation entries bind the semantic plan, model configuration and text
  digest. Identical normalized utterances can reuse validated wording.
- Cache reads verify hashes, settings and utterance constraints. Missing, changed or
  rejected entries stop publication.
- Generation derives speech durations and approval, reset, enrollment and login clocks
  from accepted text. Derived timestamps are validated in the resulting bundle.
- Private `TURN_PLANS.json` and manifest provenance accompany cached-text bundles.
  Public logs and reference data contain only emitted evidence.
- Reproducible generation requires the same accepted cache bytes. A new inference run
  may produce different wording even with the same inference seed.

## Export a portable cache

```bash
uv run pforge cache-export scenarios/llm-pilot.yaml --cache render_cache/pilot \
  --output output/pilot-portable-cache
uv run pforge generate scenarios/llm-pilot.yaml --text-mode cached_llm \
  --render-cache output/pilot-portable-cache --output output/pilot-portable-bundle
uv run pforge eval output/pilot-portable-bundle
```

Export copies accepted entries required by the scenario; raw request history stays
in the original authoring cache. Use fresh export, audit and bundle directories.

For enterprise workloads, run `scripts/test_large_scenario.py --render-cache PATH`
with a cache prepared for the selected scenario. `scripts/review_scenario.py` reports
speech durations, normalized repetition, source counts and schedule distribution.
