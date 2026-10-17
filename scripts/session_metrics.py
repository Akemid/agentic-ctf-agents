#!/usr/bin/env python3
"""Summarize per-model-call latency and token usage from strands_harness session snapshots.

Each assistant message in a snapshot carries `metadata.usage` and `metadata.metrics`, so one
snapshot is enough to tell whether an agent is slow because it generates more (output and
reasoning) or because each call is heavier (input/context).

    python scripts/session_metrics.py <snapshot.json | sessions dir> [...]
    python scripts/session_metrics.py fast=../agente-promp-directo/.agent/sessions slow=.agent/sessions

A `label=path` argument names the row; a directory is searched for `snapshot_latest.json`.
`snapshot_latest.json` is overwritten on every run: copy it aside before the next run.

`latencyMs` is reported by Bedrock (server side); `timeToFirstByteMs` is measured by Strands on
the client and can exceed it when the request waits before Bedrock starts answering.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

SNAPSHOT_NAME = "snapshot_latest.json"


@dataclass(frozen=True)
class Call:
    latency_ms: int
    ttfb_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    reasoning_chars: int
    text_chars: int
    tool_uses: int


def parse_calls(snapshot: dict) -> list[Call]:
    calls = []
    for message in snapshot.get("data", {}).get("messages", []):
        metadata = message.get("metadata")
        if message.get("role") != "assistant" or not metadata:
            continue
        usage, metrics = metadata.get("usage", {}), metadata.get("metrics", {})
        reasoning = text = tools = 0
        for block in message.get("content", []):
            if "reasoningContent" in block:
                reasoning += len(json.dumps(block["reasoningContent"]))
            elif "text" in block:
                text += len(block["text"])
            elif "toolUse" in block:
                tools += 1
        calls.append(
            Call(
                latency_ms=metrics.get("latencyMs", 0),
                ttfb_ms=metrics.get("timeToFirstByteMs", 0),
                input_tokens=usage.get("inputTokens", 0)
                + usage.get("cacheReadInputTokens", 0)
                + usage.get("cacheWriteInputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                total_tokens=usage.get("totalTokens", 0),
                reasoning_chars=reasoning,
                text_chars=text,
                tool_uses=tools,
            )
        )
    return calls


def resolve(arg: str) -> tuple[str, list[Path]]:
    label, sep, raw = arg.partition("=")
    if not sep or Path(arg).exists():
        label, raw = "", arg
    path = Path(raw)
    files = sorted(path.rglob(SNAPSHOT_NAME)) if path.is_dir() else [path]
    return label or str(path), files


def p95(values: list[int]) -> float:
    if len(values) < 2:
        return float(values[0]) if values else 0.0
    return statistics.quantiles(values, n=20, method="inclusive")[-1]


def mean(values: list[int]) -> float:
    return statistics.fmean(values) if values else 0.0


def summarize(label: str, calls: list[Call]) -> dict[str, str]:
    latencies = [c.latency_ms for c in calls]
    return {
        "agent": label,
        "calls": str(len(calls)),
        "total_s": f"{sum(latencies) / 1000:.1f}",
        "lat_avg_ms": f"{mean(latencies):.0f}",
        "lat_p95_ms": f"{p95(latencies):.0f}",
        "ttfb_avg_ms": f"{mean([c.ttfb_ms for c in calls]):.0f}",
        "in_tok_avg": f"{mean([c.input_tokens for c in calls]):.0f}",
        "out_tok_avg": f"{mean([c.output_tokens for c in calls]):.0f}",
        "reason_chr_avg": f"{mean([c.reasoning_chars for c in calls]):.0f}",
        "text_chr_avg": f"{mean([c.text_chars for c in calls]):.0f}",
        "tools_per_call": f"{mean([c.tool_uses for c in calls]):.2f}",
    }


def print_table(rows: list[dict[str, str]]) -> None:
    headers = list(rows[0])
    widths = {h: max(len(h), *(len(r[h]) for r in rows)) for h in headers}
    print("  ".join(h.ljust(widths[h]) for h in headers))
    for row in rows:
        print("  ".join(row[h].ljust(widths[h]) for h in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize model-call metrics from session snapshots.")
    parser.add_argument("sources", nargs="+", help="snapshot file or sessions dir, optionally label=path")
    args = parser.parse_args()

    rows = []
    for source in args.sources:
        label, files = resolve(source)
        if not files or not all(f.is_file() for f in files):
            print(f"no {SNAPSHOT_NAME} found for {source}", file=sys.stderr)
            return 1
        calls = [c for f in files for c in parse_calls(json.loads(f.read_text()))]
        rows.append(summarize(label, calls))
    print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
