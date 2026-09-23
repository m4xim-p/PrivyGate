#!/usr/bin/env python3
"""Analyze a py-spy raw flamegraph dump, aggregating CPU samples by category.

Categories:
  - ProcessStore: app/process_store.py (store operations, TTL, pending).
  - Detectors:    app/pii.py (detect, overlap, context, masking).
  - HTTP:         app/main.py, uvicorn, asyncio, FastAPI (request handling).
  - Other:        everything else (threading, futures, stdlib, etc.).

Usage:
  python scripts/k6/analyze_profile.py <raw_profile.txt>
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("ProcessStore", ["process_store.py", "ProcessStore"]),
    (
        "Detectors",
        [
            "app/pii.py",
            "pii.py",
            "pii_engine.py",
            "detect",
            "resolve_overlapping",
            "PIIMasker",
            "address_detector",
        ],
    ),
    (
        "HTTP",
        ["app/main.py", "main.py", "uvicorn", "asyncio", "fastapi", "starlette", "pydantic"],
    ),
]


def categorize(frame: str) -> str:
    """Classify a single stack frame into a category."""
    for category, patterns in CATEGORY_RULES:
        for pattern in patterns:
            if pattern in frame:
                return category
    return "Other"


def is_idle_worker(stack: str) -> bool:
    """True if the stack is an idle worker thread (no application work)."""
    if "_worker" not in stack:
        return False
    # Idle if no app/pii/process_store/main frames are present.
    app_frames = (
        "app/pii.py" in stack
        or "pii_engine.py" in stack
        or "process_store.py" in stack
        or "app/main.py" in stack
        or "process_service.py" in stack
    )
    return not app_frames


def classify_stack(stack: str) -> str:
    """Classify a full stack trace into a category (or 'Idle')."""
    if is_idle_worker(stack):
        return "Idle"
    frames = [f.strip() for f in stack.split(";") if f.strip()]
    for frame in reversed(frames):
        cat = categorize(frame)
        if cat != "Other":
            return cat
    return "Other"


def analyze(path: Path) -> None:
    total_samples = 0
    category_counts: Counter[str] = Counter()
    # Per-function counts within each category (top frames).
    function_counts: Counter[str] = Counter()

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "frame1;frame2;frame3 <count>" (single space before count).
        if " " not in line:
            continue
        stack, count_str = line.rsplit(" ", 1)
        try:
            count = int(count_str)
        except ValueError:
            continue
        total_samples += count

        frames = [f.strip() for f in stack.split(";") if f.strip()]
        if not frames:
            continue

        category = classify_stack(stack)
        category_counts[category] += count

        # Top function (leaf frame) for per-function breakdown.
        leaf = frames[-1]
        function_counts[f"{category}: {leaf}"] += count

    print("CPU profile breakdown (py-spy raw samples)")
    print("=" * 60)
    print(f"Total samples: {total_samples}")
    print()
    print("By category:")
    for category, count in category_counts.most_common():
        pct = (count / total_samples * 100) if total_samples else 0
        print(f"  {category:<14} {count:>8}  {pct:6.2f}%")
    print()
    print("Top functions (by category):")
    for func, count in function_counts.most_common(25):
        pct = (count / total_samples * 100) if total_samples else 0
        print(f"  {func:<60} {count:>8}  {pct:6.2f}%")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze py-spy raw flamegraph dump by category.",
    )
    parser.add_argument("profile", type=Path, help="path to py-spy raw dump")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.profile.exists():
        print(f"Profile file not found: {args.profile}", file=sys.stderr)
        return 1
    analyze(args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
