#!/usr/bin/env python3
"""cProfile-based profiling of starlet's MVT generation pipeline.

Calls starlet.generate_mvt() directly and profiles it with Python's cProfile.
Always accurate regardless of internal code changes — no manual updates needed.

Usage:
    python benchmarks/profile_mvt_stages.py --dataset-dir path/to/dataset

    # Compare two saved profiles:
    python benchmarks/compare_profiles.py --baseline old.json --current new.json
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Default dataset directory — produced by running `starlet tile` on any input.
# Override with --dataset-dir on the command line.
DEFAULT_DATASET_DIR = PROJECT_ROOT / "benchmark_output_parquet"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, cwd=PROJECT_ROOT,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# ---------------------------------------------------------------------------
# cProfile mode
# ---------------------------------------------------------------------------

def _build_highlight_functions() -> frozenset[str]:
    """Build the cProfile highlight set from IntermediateVectorTile's public API.

    All public methods of IntermediateVectorTile are included automatically, so
    this stays correct across renames and additions without any manual edits.
    A small set of pipeline-level anchors is always included regardless.
    """
    pipeline_anchors = frozenset({"run", "from_wkb", "make_valid"})
    try:
        import inspect
        from starlet._internal.mvt.intermediate_tile import IntermediateVectorTile
        tile_methods = frozenset(
            name
            for name, obj in inspect.getmembers(IntermediateVectorTile, predicate=inspect.isfunction)
            if not name.startswith("_")
        )
        return pipeline_anchors | tile_methods
    except Exception:
        # starlet not installed yet, or module path changed — fall back to known names
        return pipeline_anchors | frozenset({
            "add_feature", "merge", "encode",
            "simplify_geometry", "write_features", "load_features",
        })


# Built once at module load; printed at startup so the user can see what's tracked.
_HIGHLIGHT_FUNCTIONS = _build_highlight_functions()


def _safe_parallelism() -> int:
    """Return a safe worker count for generate_mvt() on the current machine.

    Windows spawns processes via CreateProcess which is heavier than fork and
    exhausts the pagefile quickly; cap at 2.  On Linux/macOS fork is cheap and
    memory consumption is copy-on-write, so more workers are fine up to
    min(cpu_count, 4).  A psutil check tightens the cap further when RAM is low.
    """
    cpu = os.cpu_count() or 2
    try:
        import psutil
        # Assume each worker peak-uses ~2 GB for geometry processing
        mem_cap = max(1, int(psutil.virtual_memory().available / (2 * 1024 ** 3)))
    except ImportError:
        mem_cap = cpu

    if sys.platform == "win32":
        platform_cap = 2
    else:
        platform_cap = 4

    return min(cpu, mem_cap, platform_cap)


def _run_generate_mvt(dataset_dir: Path, zoom: int, threshold: float, parallelism: int):
    """Call starlet.generate_mvt(), halving parallelism on BrokenProcessPool until p=1."""
    import starlet

    current_p = parallelism
    while True:
        try:
            return starlet.generate_mvt(
                tile_dir=str(dataset_dir),
                zoom=zoom,
                threshold=threshold,
                parallelism=current_p,
            ), current_p
        except BrokenProcessPool:
            if current_p <= 1:
                raise
            next_p = max(1, current_p // 2)
            print(f"  [retry] BrokenProcessPool at parallelism={current_p} — retrying with {next_p}")
            current_p = next_p


def _profile_cprofile(dataset_dir: Path, zoom: int, threshold: float, parallelism: int = 0) -> dict:
    """Profile generate_mvt() with cProfile.

    parallelism=0 (default) triggers auto-detection via _safe_parallelism().
    Pass an explicit positive integer to override.
    """
    if parallelism <= 0:
        parallelism = _safe_parallelism()
    print(f"  Parallelism: {parallelism} (platform={sys.platform}, cpus={os.cpu_count()})")

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    mvt_result, used_p = _run_generate_mvt(dataset_dir, zoom, threshold, parallelism)
    pr.disable()
    total_time = time.perf_counter() - t0
    if used_p != parallelism:
        print(f"  [note] final parallelism used: {used_p}")

    stream = StringIO()
    ps = pstats.Stats(pr, stream=stream).sort_stats("cumulative")
    ps.print_stats(40)
    profile_text = stream.getvalue()

    # Extract timing for highlighted functions.
    # pstats.Stats.stats is a dict keyed by (file, lineno, funcname).
    # The value tuple is (primitive_calls, total_calls, tottime, cumtime, callers).
    # tottime = time spent inside the function itself (excludes callees).
    # cumtime = total time including all callees — use this to rank hot spots.
    highlights: dict[str, dict] = {}
    ps2 = pstats.Stats(pr)
    for func, (cc, nc, tt, ct, _) in ps2.stats.items():
        fname = func[2]
        if fname in _HIGHLIGHT_FUNCTIONS:
            highlights[fname] = {
                "calls": nc,
                "tottime_s": round(tt, 4),
                "cumtime_s": round(ct, 4),
            }

    return {
        "mode": "cprofile",
        "total_wall_time_s": round(total_time, 3),
        "mvt_tiles_generated": mvt_result.tile_count,
        "zoom_levels": mvt_result.zoom_levels,
        "highlights": highlights,
        "full_profile_text": profile_text,
    }


def _print_cprofile_report(result: dict):
    print()
    print(f"Total wall time: {result['total_wall_time_s']:.3f}s")
    print(f"MVT tiles generated: {result['mvt_tiles_generated']:,}")
    print(f"Zoom levels: {result['zoom_levels']}")
    print()
    print("Key function timings:")
    print(f"{'Function':<35} | {'Calls':>8} | {'tottime (s)':>12} | {'cumtime (s)':>12}")
    print("-" * 74)
    for fname, stats in sorted(
        result["highlights"].items(), key=lambda kv: -kv[1]["cumtime_s"]
    ):
        print(
            f"{fname:<35} | {stats['calls']:>8,} | "
            f"{stats['tottime_s']:>12.4f} | {stats['cumtime_s']:>12.4f}"
        )
    print()
    print("--- Full cProfile output (top 40 by cumulative time) ---")
    print(result["full_profile_text"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Profile MVT generation. Use --mode cprofile for automatic accuracy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset-dir",
        default=str(DEFAULT_DATASET_DIR),
        help=(
            "Dataset directory containing parquet_tiles/ and histograms/ "
            f"(default: {DEFAULT_DATASET_DIR})"
        ),
    )
    parser.add_argument("--zoom", type=int, default=5, help="Maximum zoom level (default: 5)")
    parser.add_argument("--threshold", type=float, default=50_000, help="Histogram density threshold (default: 50000)")
    parser.add_argument(
        "--parallelism",
        type=int,
        default=0,
        help=(
            "Worker processes for cprofile mode. "
            "0 (default) = auto-detect: <=2 on Windows, <=4 on Linux/macOS, "
            "capped by available RAM. Retries with half on BrokenProcessPool."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(SCRIPT_DIR / "results" / "mvt_stage_profile.json"),
        help="Path to write JSON results",
    )
    args = parser.parse_args()

    dataset_dir = Path(args.dataset_dir).resolve()
    parquet_dir = dataset_dir / "parquet_tiles"

    if not dataset_dir.exists():
        raise SystemExit(f"Dataset directory not found: {dataset_dir}")
    if not parquet_dir.exists():
        raise SystemExit(f"No parquet_tiles/ found under {dataset_dir}")

    resolved_p = args.parallelism if args.parallelism > 0 else _safe_parallelism()
    print(f"Dataset     : {dataset_dir}")
    print(f"Zoom        : {args.zoom}")
    print(f"Threshold   : {args.threshold}")
    print(f"Parallelism : {resolved_p} (--parallelism={args.parallelism}, platform={sys.platform})")
    print(f"Tracking    : {sorted(_HIGHLIGHT_FUNCTIONS)}")
    print()

    result = _profile_cprofile(dataset_dir, args.zoom, args.threshold, args.parallelism)
    _print_cprofile_report(result)

    result["metadata"] = {
        "mode": "cprofile",
        "git_commit": _get_git_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "args": {
            "dataset_dir": str(dataset_dir),
            "zoom": args.zoom,
            "threshold": args.threshold,
        },
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Don't save the full profile text in JSON — it's large and not useful for compare_profiles.py
    result_to_save = {k: v for k, v in result.items() if k != "full_profile_text"}
    out_path.write_text(json.dumps(result_to_save, indent=2))
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
