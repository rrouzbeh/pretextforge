# Development guide

PretextForge generates scenario-based datasets for testing social-engineering
detection. Its EvidenceForge-inspired workflow combines routine activity and
scripted cases into correlated evidence with a separate answer key.

## Architecture

Keep scenario validation, action expansion, text authoring and bundle validation
separate. The CLI exposes `validate`, `plan`, `render`, `generate`, `eval`, `audit`
and `cache-export`. Detection systems consume the resulting public evidence through
the [documented source contracts](reference.md).

Action builders own shared state: identities, active factors, sessions, approvals,
file objects and event clocks. Emitters serialize those facts into six sources.
Observation profiles apply source loss and delay after canonical events are built.

Optional LLM authoring produces a validated text cache. Generation reads accepted
entries and derives speech durations and downstream clocks from their wording.
Model access belongs exclusively to the authoring step.

## Engineering invariants

1. **Determinism.** Generation makes no model, network or wall-clock calls. Fixed
   scenario, seed and accepted text produce identical bundles across fresh processes.
2. **Shared facts.** Compute cross-source identities and timestamps in action builders,
   not independently in emitters.
3. **Collection semantics.** Event time and availability time are distinct. Collection
   changes preserve canonical history and private labels. Arrival ties have stable order.
4. **Evidence separation.** Public logs and reference data exclude malicious intent,
   true actors, case labels and private turn plans.
5. **Text validation.** Model output may change wording while preserving participants,
   required literals, verification state and configured outcomes. Validate every restored
   utterance before publishing a bundle.
6. **Contract maintenance.** Update validation, documentation and relevant invariant
   tests together when changing schemas.
7. **Output preservation.** Generate into a fresh directory. Keep existing datasets
   and caches available for reproduction.

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Exercise template and packaged cached-text generation offline. For changes to action
builders or source contracts, also run the [scenario sweep and large workload](reproduction.md).
Those checks cover both collection profiles, independent evaluation, fresh-process
byte repeats and a baseline-only control.

## Contributions

Keep changes focused on scenario authoring, evidence generation or dataset quality.
Add an example and update the relevant contract when introducing a behavior. Use
synthetic identities and example domains in fixtures. Keep model weights, runtime
caches and generated datasets outside version control.

See [planned work](../TODO.md) and [recorded validation results](validation.md).
