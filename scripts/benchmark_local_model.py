"""Measure a local renderer's JSON throughput; never used by dataset generation."""

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

from pretextforge.rendering import RENDER_PROMPT, Endpoint, ModelConfig


def benchmark(endpoint: str, model: str, revision: str) -> dict:
    config = ModelConfig(model=model, revision=revision)
    client = Endpoint(endpoint, config)
    lines = [
        "My name is SLOT_0. Please help me reconnect to the VPN.",
        "Please restart the VPN client and try signing in again.",
        "The VPN is working again. Thank you for helping.",
        "Thanks for confirming. I will close this support ticket.",
        "I lost my phone and need to reset my MFA setup.",
        "We need your manager to approve this before the reset.",
        "Please contact my manager for confirmation.",
        "Approval has not arrived. The reset remains on hold.",
    ]
    fields = {f"turn_{i}": line for i, line in enumerate(lines, 1)}
    schema = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in fields},
        "required": list(fields),
        "additionalProperties": False,
    }
    results = []
    for trial in range(4):
        request = {
            "model": model,
            "messages": [
                {"role": "system", "content": RENDER_PROMPT},
                {"role": "user", "content": json.dumps({"turns": fields, "style": "concise"})},
            ],
            "temperature": config.temperature,
            "seed": config.seed + trial,
            "max_tokens": config.max_tokens,
            "response_format": {"type": "json_object", "schema": schema},
        }
        start = time.perf_counter()
        with client.opener.open(
            urllib.request.Request(
                client.url,
                json.dumps(request).encode(),
                {"Content-Type": "application/json"},
            ),
            timeout=240,
        ) as response:
            body = json.load(response)
        elapsed = time.perf_counter() - start
        content = json.loads(body["choices"][0]["message"]["content"])
        if set(content) != set(fields) or not all(isinstance(v, str) for v in content.values()):
            raise ValueError("renderer did not return the requested JSON string fields")
        results.append(
            {
                "warmup": trial == 0,
                "seconds": elapsed,
                "output_tokens_per_second_including_prompt": (
                    body["usage"]["completion_tokens"] / elapsed
                ),
                "usage": body["usage"],
                "timings": body.get("timings", {}),
                "response": content,
            }
        )
    return {
        "config": config.model_dump(),
        "trials": results,
        "median_output_tokens_per_second_including_prompt": statistics.median(
            r["output_tokens_per_second_including_prompt"] for r in results[1:]
        ),
        "note": "One warmup, three sequential eight-line JSON batches; not a quality benchmark.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as stream:
        result = benchmark(args.endpoint, args.model, args.revision)
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "trials"}, indent=2))
