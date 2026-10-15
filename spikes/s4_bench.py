"""Task 0.7 / S4: latency and dtype-agreement benchmark for the decider.

Talks to a running `strands-decider serve` (--url) OR a SageMaker endpoint (--endpoint).
Replays real ticks extracted from decider_loop logs with the same 3-question request.

  # latency of one server (fp16 or fp32), saves answers for the comparison
  python spikes/s4_bench.py run --log amazingplayer-python/logs/decider-194227.log \
      --url http://127.0.0.1:8099 --n 20 --out answers-fp16.json
  # SageMaker variant
  python spikes/s4_bench.py run --log ... --endpoint ctf-decider --region us-east-2 --n 20 --out a.json
  # fp16 vs fp32 agreement gate (+-0.05)
  python spikes/s4_bench.py compare answers-fp16.json answers-fp32.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s4_lib  # noqa: E402


def _post_url(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/systemone", data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _make_sagemaker(endpoint: str, region: str):
    import boto3
    from botocore.config import Config

    client = boto3.client("sagemaker-runtime", region_name=region,
                          config=Config(connect_timeout=1, read_timeout=30, retries={"max_attempts": 0}))

    def post(_url: str, body: dict) -> dict:
        out = client.invoke_endpoint(EndpointName=endpoint, ContentType="application/json",
                                     Body=json.dumps(body).encode())
        return json.loads(out["Body"].read())

    return post


def cmd_run(args: argparse.Namespace) -> int:
    states = s4_lib.parse_states(Path(args.log).read_text())
    try:
        states = s4_lib.select_usable_states(states, args.n)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if len(states) < args.n:
        print(f"only {len(states)} usable states in the log (wanted {args.n})")
    post = _make_sagemaker(args.endpoint, args.region) if args.endpoint else _post_url
    target = args.endpoint or args.url
    print("warm-up call (not counted)...")
    post(target, s4_lib.build_request(states[0]["state"], states[0]["options"]))
    latencies, answers = [], []
    for item in states:
        body = s4_lib.build_request(item["state"], item["options"])
        t0 = time.perf_counter()
        resp = post(target, body)
        latencies.append((time.perf_counter() - t0) * 1000)
        answers.append(resp["answers"])
    p50, p95 = s4_lib.percentile(latencies, 50), s4_lib.percentile(latencies, 95)
    print(f"n={len(latencies)} p50={p50:.0f} ms p95={p95:.0f} ms max={max(latencies):.0f} ms")
    print("S4 LATENCY GATE (p95 <= 1000 ms):", "PASS" if p95 <= 1000 else "FAIL (>2500 ms means breaker-first LLM path)")
    Path(args.out).write_text(json.dumps({"latencies_ms": latencies, "answers": answers}))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    a = json.loads(Path(args.a).read_text())["answers"]
    b = json.loads(Path(args.b).read_text())["answers"]
    try:
        s4_lib.check_comparable(a, b)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    n = len(a)
    worst = max(s4_lib.max_prob_delta(a[i], b[i]) for i in range(n))
    print(f"states compared={n} worst delta={worst}")
    print("S4 DTYPE GATE (<= 0.05):", "PASS" if worst <= 0.05 else "FAIL -> use fp32")
    return 0 if worst <= 0.05 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--log", required=True)
    run.add_argument("--url", default="http://127.0.0.1:8099")
    run.add_argument("--endpoint")
    run.add_argument("--region", default="us-east-2")
    run.add_argument("--n", type=int, default=20)
    run.add_argument("--out", required=True)
    run.set_defaults(func=cmd_run)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("a")
    cmp_.add_argument("b")
    cmp_.set_defaults(func=cmd_compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
