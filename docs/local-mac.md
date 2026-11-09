# Local text authoring on Apple Silicon

This configuration was tested with Qwen3.5-4B Q4_K_M on an Apple M4 Mac mini
(10 GPU cores, 16 GB unified memory) running macOS 15.6.1. The model uses llama.cpp's
Metal backend with a 4,096-token context and reasoning disabled.

| Component | Tested configuration |
| --- | --- |
| Model | [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) |
| Quantization | [Unsloth GGUF](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF), Q4_K_M, 2.55 GiB |
| Model revision | `e87f176479d0855a907a41277aca2f8ee7a09523` |
| Runtime | llama.cpp 0.8.0, build 11960, commit `8642541b2` |

## Install the runtime and weights

Run from the repository root. Model files are excluded from Git.

```bash
brew install llama.cpp
mkdir -p .pforge-models
curl -fL --retry 3 \
  -o .pforge-models/Qwen3.5-4B-Q4_K_M.gguf.partial \
  https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/e87f176479d0855a907a41277aca2f8ee7a09523/Qwen3.5-4B-Q4_K_M.gguf
mv .pforge-models/Qwen3.5-4B-Q4_K_M.gguf.partial .pforge-models/Qwen3.5-4B-Q4_K_M.gguf
```

## Start the server

Run the server in a dedicated terminal:

```bash
GGML_METAL_BF16_DISABLE=1 llama-server \
  --model .pforge-models/Qwen3.5-4B-Q4_K_M.gguf \
  --alias qwen3.5-4b-q4_k_m --host 127.0.0.1 --port 8080 \
  --ctx-size 4096 --parallel 1 --gpu-layers all --flash-attn on --reasoning off
```

`GGML_METAL_BF16_DISABLE=1` avoids the bfloat vector-cast compilation error observed
with the tested macOS 15 runtime, while retaining Metal acceleration. The endpoint
must support schema-constrained JSON responses.

Check readiness from another terminal:

```bash
curl -fsS http://127.0.0.1:8080/v1/models
```

## Author and generate

```bash
uv run pforge render scenarios/benign-heavy-week.yaml \
  --cache render_cache/northstar-qwen35 \
  --model qwen3.5-4b-q4_k_m --revision e87f176479d0855a907a41277aca2f8ee7a09523
uv run pforge audit scenarios/benign-heavy-week.yaml \
  --cache render_cache/northstar-qwen35 --output output/northstar-audit
uv run python scripts/test_large_scenario.py --scenario scenarios/benign-heavy-week.yaml \
  --render-cache render_cache/northstar-qwen35 --output output/northstar-cached
```

Rendering uses the model server. Audit, generation and quality checks run offline.
Stop the server with Ctrl-C after authoring to release its memory. Reuse accepted
cache entries with the same model settings; choose fresh bundle and audit directories.

## Measured throughput

An eight-line JSON authoring workload used one warmup request and three measured
requests. Median decode throughput was **27.73 tokens/s**; median output throughput
including prompt processing was **27.54 tokens/s**. The first uncached request
achieved 22.54 tokens/s overall. Later requests reused a 299-token prompt prefix;
responses contained 125–132 tokens per batch.

These measurements describe the configuration above. The benchmark returned unchanged
source lines, so its throughput measures JSON generation rather than paraphrase variety.
To measure another run, use a fresh output path:

```bash
mkdir -p output
uv run python scripts/benchmark_local_model.py \
  --model qwen3.5-4b-q4_k_m --revision e87f176479d0855a907a41277aca2f8ee7a09523 \
  --output output/qwen35-benchmark.json
```

The script records raw responses, token usage and available server timings. See
[validation results](validation.md) for the separately measured 500-user dataset run
and [the authoring workflow](llm-workflow.md) for cache and validation details.
