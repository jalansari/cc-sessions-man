"""Benchmark classification speed and accuracy across batch sizes and parallelism.

Takes a fixed sample of finished sessions and classifies it once per configuration
(batch size x parallel calls), using the same `claude -p` call as the app. Accuracy is
measured against a reference made with batch size 1. The reference is run twice, so the
model's own run-to-run noise shows up as a baseline.

Nothing is written to the app's caches. Results are printed as a Markdown table and
saved as JSON under benchmarks/results/.

    uv run python benchmarks/classify_batch_size.py
    uv run python benchmarks/classify_batch_size.py --sample 40 --batch-sizes 5 20 --jobs 8
    uv run python benchmarks/classify_batch_size.py --sample 160 --batch-sizes 5 10 --jobs 8 16 --no-reference
"""

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from cc_sessions_man.classify import LABELS, batch_prompt, call_claude
from cc_sessions_man.cli import positive_int
from cc_sessions_man.scan import CLAUDE_HOME, ENDED, Session, scan

RESULTS_DIR = Path(__file__).parent / "results"
NEEDS_YOU = {"decision", "action"}


def run_config(sample: list[Session], batch_size: int, jobs: int, model: str) -> dict:
    """Classify `sample` once. Returns labels plus timing, cost and failure counts."""
    batches = [sample[i : i + batch_size] for i in range(0, len(sample), batch_size)]
    labels: dict[str, str] = {}
    cost = 0.0
    failed_calls = 0
    call_times = []
    first_result = None
    t0 = time.monotonic()

    def work(batch):
        t = time.monotonic()
        try:
            out = call_claude(batch_prompt(batch), model)
        except Exception as exc:
            return batch, None, time.monotonic() - t, exc
        return batch, out, time.monotonic() - t, None

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for future in as_completed(pool.submit(work, b) for b in batches):
            batch, out, took, err = future.result()
            call_times.append(took)
            if err:
                failed_calls += 1
                continue
            first_result = first_result or time.monotonic() - t0
            cost += out.get("total_cost_usd") or 0.0
            data = out.get("structured_output") or json.loads(out["result"])
            ids = {s.session_id for s in batch}
            for item in data.get("items", []):
                if item.get("id") in ids and item.get("label") in LABELS:
                    labels[item["id"]] = item["label"]

    wall = time.monotonic() - t0
    return {
        "batch_size": batch_size,
        "jobs": jobs,
        "calls": len(batches),
        "failed_calls": failed_calls,
        "labelled": len(labels),
        "wall_s": round(wall, 2),
        "first_result_s": round(first_result, 2) if first_result else None,
        "sessions_per_s": round(len(labels) / wall, 2) if wall else 0,
        "mean_call_s": round(sum(call_times) / len(call_times), 2) if call_times else None,
        "cost_usd": round(cost, 4),
        "labels": labels,
    }


def agreement(a: dict[str, str], b: dict[str, str]) -> tuple[float | None, float | None]:
    """(exact label agreement, agreement on 'needs you' vs not) over sessions both labelled."""
    common = a.keys() & b.keys()
    if not common:
        return None, None
    exact = sum(a[k] == b[k] for k in common) / len(common)
    needs = sum((a[k] in NEEDS_YOU) == (b[k] in NEEDS_YOU) for k in common) / len(common)
    return round(exact, 3), round(needs, 3)


def pick_sample(n: int, seed: int) -> list[Session]:
    ended = [s for s in scan(CLAUDE_HOME) if s.state == ENDED]
    rng = random.Random(seed)
    return rng.sample(ended, min(n, len(ended)))


def fmt(v, suffix=""):
    return "–" if v is None else f"{v}{suffix}"


def pct(v):
    return "–" if v is None else f"{v * 100:.0f}%"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample", type=positive_int, default=60, help="sessions to classify per run (default: 60)")
    p.add_argument("--batch-sizes", type=positive_int, nargs="+", default=[1, 5, 10, 20, 40])
    p.add_argument("--jobs", type=positive_int, nargs="+", default=[4, 8], help="parallel calls to try (default: 4 8)")
    p.add_argument("--reference-jobs", type=positive_int, default=8, help="parallel calls for the batch-1 reference runs")
    p.add_argument("--model", default="haiku")
    p.add_argument("--seed", type=int, default=1, help="random seed for picking the sample")
    p.add_argument("--no-reference", action="store_true", help="skip the batch-1 reference runs (speed only, no accuracy)")
    args = p.parse_args()

    sample = pick_sample(args.sample, args.seed)
    print(f"Sample: {len(sample)} finished sessions (seed {args.seed}), model {args.model}\n")

    ref = ref2 = None
    noise_exact = noise_needs = None
    if not args.no_reference:
        print("Reference: batch size 1, run twice…", flush=True)
        ref = run_config(sample, 1, args.reference_jobs, args.model)
        ref2 = run_config(sample, 1, args.reference_jobs, args.model)
        noise_exact, noise_needs = agreement(ref["labels"], ref2["labels"])
        print(f"  model noise: {pct(noise_exact)} exact, {pct(noise_needs)} needs-you agreement between two batch-1 runs\n")

    runs = []
    for jobs in args.jobs:
        for bs in args.batch_sizes:
            if ref and bs == 1 and jobs == args.reference_jobs:
                r = ref  # already measured
            else:
                print(f"Batch {bs:>3}, {jobs} parallel…", flush=True)
                r = run_config(sample, bs, jobs, args.model)
            if ref:
                r["agree_exact"], r["agree_needs_you"] = agreement(ref2["labels"] if r is ref else ref["labels"], r["labels"])
            else:
                r["agree_exact"] = r["agree_needs_you"] = None
            runs.append(r)

    print("\n| Batch | Parallel | Calls | Wall | First result | Sessions/s | Mean call | Labelled | Agree (exact) | Agree (needs you) | Cost |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in runs:
        print(
            f"| {r['batch_size']} | {r['jobs']} | {r['calls']} | {r['wall_s']}s | {fmt(r['first_result_s'], 's')} "
            f"| {r['sessions_per_s']} | {fmt(r['mean_call_s'], 's')} | {r['labelled']}/{len(sample)} "
            f"| {pct(r['agree_exact'])} | {pct(r['agree_needs_you'])} | ${r['cost_usd']:.3f} |"
        )
    if ref:
        print(f"\nBaseline noise (batch 1 vs batch 1): {pct(noise_exact)} exact, {pct(noise_needs)} needs-you.")
        print("Agreement is against the batch-1 reference; the reference row is compared with its repeat run.")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "date": datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "sample_size": len(sample),
        "seed": args.seed,
        "reference": not args.no_reference,
        "noise": {"exact": noise_exact, "needs_you": noise_needs},
        "runs": [{k: v for k, v in r.items() if k != "labels"} for r in runs],
    }, indent=2))
    print(f"Saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
