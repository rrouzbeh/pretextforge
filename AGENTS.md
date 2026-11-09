# Working on PretextForge

Keep the PoC small. Read README.md and docs/development-plan.md before changing scope.
Add no model/network/wall-clock calls to generation. Compute shared facts in action
builders, not emitters. Keep private labels out of public logs and reference data. Changes to source
contracts must update validation, docs and relevant invariant tests together.

Use `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest -q`.
Do not overwrite existing generated bundles; use a new output directory.
