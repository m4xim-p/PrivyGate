#!/usr/bin/env python3
"""Real-time server metrics collector for the k6 /process load test.

Polls the gateway ``/metrics`` endpoint every second and samples the gateway
process CPU/RSS via psutil, printing a live table. Run alongside k6 to observe
server-side health (store size, event loop delay, counters) in real time.

Usage:
  python scripts/k6/metrics_collector.py \
      --metrics-url http://localhost:8000/metrics \
      --pid <gateway_pid> \
      --interval 1.0 \
      --duration 120

Requires psutil (dev dependency).
"""

from __future__ import annotations

import argparse
import time

import httpx
import psutil

DEFAULT_METRICS_URL = "http://localhost:8000/metrics"
DEFAULT_INTERVAL = 1.0


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_prometheus(body: str) -> dict[str, float]:
    """Parse Prometheus text format into {metric_name: value}."""
    metrics: dict[str, float] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            metrics[name] = float(parts[1])
        except ValueError:
            continue
    return metrics


def sample_process(pid: int | None) -> dict[str, float]:
    """Sample CPU percent and RSS bytes of a process by PID."""
    if pid is None:
        return {"cpu_percent": float("nan"), "rss_bytes": float("nan")}
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=None)
        rss = proc.memory_info().rss
        return {"cpu_percent": cpu, "rss_bytes": float(rss)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"cpu_percent": float("nan"), "rss_bytes": float("nan")}


def sample_container(container: str | None) -> dict[str, float]:
    """Sample CPU percent and RSS bytes of a Docker container via `docker stats`."""
    if container is None:
        return {"cpu_percent": float("nan"), "rss_bytes": float("nan")}
    try:
        import subprocess  # nosec B404 - dev-only metrics collector, fixed args

        out = subprocess.run(  # nosec B603 B607 - fixed arg list, no shell
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}|{{.MemUsage}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        cpu_str, mem_str = out.split("|", 1)
        cpu = float(cpu_str.rstrip("%"))
        mem_mib = float(mem_str.split("/", 1)[0].strip().rstrip("MiB"))
        return {"cpu_percent": cpu, "rss_bytes": mem_mib * 1024 * 1024}
    except Exception:
        return {"cpu_percent": float("nan"), "rss_bytes": float("nan")}


def render_row(
    elapsed: float,
    metrics: dict[str, float],
    proc: dict[str, float],
) -> str:
    def _fmt(key: str, suffix: str = "") -> str:
        value = metrics.get(key)
        if value is None:
            return "n/a"
        return f"{value:.0f}{suffix}"

    cpu = proc.get("cpu_percent")
    rss = proc.get("rss_bytes")
    cpu_s = "n/a" if cpu is None or cpu != cpu else f"{cpu:.1f}%"
    rss_s = "n/a" if rss is None or rss != rss else f"{rss / (1024 * 1024):.1f}MiB"

    return (
        f"{elapsed:6.1f}s | "
        f"mask={_fmt('privygate_process_mask_total'):>6} "
        f"demask={_fmt('privygate_process_demask_total'):>6} "
        f"retry={_fmt('privygate_process_retry_total'):>5} "
        f"429={_fmt('privygate_process_rate_limited_total'):>5} "
        f"new_id={_fmt('privygate_process_new_id_total'):>6} | "
        f"sessions={_fmt('privygate_store_sessions'):>5} "
        f"pending={_fmt('privygate_store_pending'):>4} "
        f"tomb={_fmt('privygate_store_tombstones'):>5} "
        f"bytes={_fmt('privygate_store_bytes'):>8} | "
        f"loop={_fmt('privygate_event_loop_delay_ms', 'ms'):>7} | "
        f"cpu={cpu_s:>6} rss={rss_s:>8}"
    )


def print_header() -> None:
    print(
        "  time | mask demask retry  429 new_id | sess pend tomb    bytes | "
        "loop(ms) |    cpu     rss"
    )
    print("-" * 100)


async def run(
    metrics_url: str,
    pid: int | None,
    container: str | None,
    interval: float,
    duration: float,
) -> int:
    print_header()
    started_at = time.monotonic()
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            elapsed = time.monotonic() - started_at
            if elapsed >= duration:
                break

            metrics: dict[str, float] = {}
            try:
                resp = await client.get(metrics_url)
                if resp.status_code == 200:
                    metrics = parse_prometheus(resp.text)
            except httpx.HTTPError:
                pass

            proc = (
                sample_container(container)
                if container is not None
                else sample_process(pid)
            )
            print(render_row(elapsed, metrics, proc), flush=True)

            await asyncio_sleep(interval)

    return 0


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Real-time server metrics collector for k6 /process load test.",
    )
    parser.add_argument(
        "--metrics-url",
        default=DEFAULT_METRICS_URL,
        help=f"/metrics URL, default: {DEFAULT_METRICS_URL}",
    )
    parser.add_argument(
        "--pid",
        type=positive_int,
        default=None,
        help="gateway process PID for CPU/RSS sampling (host process, optional)",
    )
    parser.add_argument(
        "--container",
        default=None,
        help="Docker container name for CPU/RSS sampling via `docker stats` (optional)",
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=DEFAULT_INTERVAL,
        help=f"poll interval in seconds, default: {DEFAULT_INTERVAL}",
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        default=120.0,
        help="collection duration in seconds, default: 120",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio_run(args)


def asyncio_run(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(
        run(args.metrics_url, args.pid, args.container, args.interval, args.duration)
    )


if __name__ == "__main__":
    raise SystemExit(main())
