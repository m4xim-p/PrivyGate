#!/usr/bin/env python3
"""Rate-controlled load test for the /process contract.

Matches the AlfaSonar evaluation profile from docs/evaluation-contract.md:
- ~5 minute run with ramp-up;
- average ~330 RPS, peaks up to 1000 RPS;
- up to 200 concurrent connections, each waits for its response;
- masking and demasking roughly equal;
- target latency <= 1s, reported as mean/p50/p95/p99;
- hard timeout 10s;
- up to two retries after the first attempt;
- 429 is not an error (waits on Retry-After) but is recorded.

Latency is measured per single HTTP request (masking and demasking separately),
not per pair. Uses only synthetic PII.
"""

import argparse
import asyncio
import contextlib
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

DEFAULT_URL = "http://localhost:8000/process"
DEFAULT_DURATION = 300.0
DEFAULT_TARGET_RPS = 330.0
DEFAULT_PEAK_RPS = 1000.0
DEFAULT_RAMP_SECONDS = 60.0
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_KEEPALIVE = 10


@dataclass(frozen=True, slots=True)
class RequestResult:
    success: bool
    latency: float
    op: str
    status: int
    retries: int = 0
    error_type: str | None = None
    phase: str = "peak"


@dataclass(slots=True)
class Stats:
    results: list[RequestResult] = field(default_factory=list)
    rate_limited: int = 0
    phase_counts: dict[str, int] = field(default_factory=dict)


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def synthetic_payload(request_number: int) -> str:
    phone_suffix = request_number % 100
    return (
        f"Иванов Иван Иванович, паспорт 45 10 {request_number % 1000000:06d}, "
        f"телефон +7 999 000-00-{phone_suffix:02d}, "
        f"email loadtest-{request_number}@example.com"
    )


class RateLimiter:
    """Token-bucket rate limiter with a time-varying target rate."""

    def __init__(self, start_rps: float, peak_rps: float, ramp_seconds: float) -> None:
        self._start_rps = start_rps
        self._peak_rps = peak_rps
        self._ramp_seconds = ramp_seconds
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _current_rps(self, now: float) -> float:
        if self._ramp_seconds <= 0:
            return self._peak_rps
        elapsed = now - self._start_time
        if elapsed >= self._ramp_seconds:
            return self._peak_rps
        fraction = elapsed / self._ramp_seconds
        return self._start_rps + (self._peak_rps - self._start_rps) * fraction

    async def acquire(self) -> None:
        self._start_time = getattr(self, "_start_time", time.monotonic())
        while True:
            async with self._lock:
                now = time.monotonic()
                rps = self._current_rps(now)
                self._tokens = min(self._tokens + (now - self._last) * rps, rps)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / rps
            await asyncio.sleep(wait)


async def send_with_retries(
    client: httpx.AsyncClient,
    url: str,
    payload: str,
    payload_id: str,
    max_retries: int,
    timeout: float,
) -> tuple[httpx.Response, int]:
    """Send a request with up to max_retries retries; 429 waits on Retry-After."""
    attempt = 0
    while True:
        try:
            resp = await client.post(
                url,
                json={"payload": payload, "payload_id": payload_id},
                timeout=timeout,
            )
        except httpx.HTTPError:
            if attempt >= max_retries:
                raise
            attempt += 1
            await asyncio.sleep(0.1)
            continue

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(retry_after)
            if attempt >= max_retries:
                return resp, attempt
            attempt += 1
            continue

        if resp.status_code >= 500 and attempt < max_retries:
            attempt += 1
            await asyncio.sleep(0.1)
            continue

        return resp, attempt


async def execute_pair(
    client: httpx.AsyncClient,
    url: str,
    request_number: int,
    limiter: RateLimiter,
    stats: Stats,
    max_retries: int,
    timeout: float,
    phase: str,
) -> None:
    payload_id = f"bench-{request_number}"
    payload = synthetic_payload(request_number)

    # Masking.
    await limiter.acquire()
    started = time.perf_counter()
    try:
        masked_resp, mask_retries = await send_with_retries(
            client, url, payload, payload_id, max_retries, timeout
        )
        mask_latency = time.perf_counter() - started
        if masked_resp.status_code == 429:
            stats.rate_limited += 1
            return
        if masked_resp.status_code != 200:
            stats.results.append(
                RequestResult(
                    success=False,
                    latency=mask_latency,
                    op="mask",
                    status=masked_resp.status_code,
                    retries=mask_retries,
                    error_type=f"HTTP{masked_resp.status_code}",
                    phase=phase,
                )
            )
            return
        masked = masked_resp.json()["result"]
        stats.results.append(
            RequestResult(
                success=True,
                latency=mask_latency,
                op="mask",
                status=200,
                retries=mask_retries,
                    phase=phase,
                )
        )
    except Exception as exc:
        stats.results.append(
            RequestResult(
                success=False,
                latency=time.perf_counter() - started,
                op="mask",
                status=0,
                retries=mask_retries if "mask_retries" in locals() else 0,
                error_type=type(exc).__name__,
                    phase=phase,
                )
        )
        return

    # Demasking.
    await limiter.acquire()
    started = time.perf_counter()
    try:
        orig_resp, demask_retries = await send_with_retries(
            client, url, masked, payload_id, max_retries, timeout
        )
        demask_latency = time.perf_counter() - started
        if orig_resp.status_code == 429:
            stats.rate_limited += 1
            return
        if orig_resp.status_code != 200:
            stats.results.append(
                RequestResult(
                    success=False,
                    latency=demask_latency,
                    op="demask",
                    status=orig_resp.status_code,
                    retries=demask_retries,
                    error_type=f"HTTP{orig_resp.status_code}",
                    phase=phase,
                )
            )
            return
        original = orig_resp.json()["result"]
        ok = original == payload
        stats.results.append(
            RequestResult(
                success=ok,
                latency=demask_latency,
                op="demask",
                status=200,
                retries=demask_retries,
                error_type=None if ok else "RoundTripMismatch",
                    phase=phase,
                )
        )
    except Exception as exc:
        stats.results.append(
            RequestResult(
                success=False,
                latency=time.perf_counter() - started,
                op="demask",
                status=0,
                retries=demask_retries if "demask_retries" in locals() else 0,
                error_type=type(exc).__name__,
                    phase=phase,
                )
        )


def print_report(stats: Stats, total_duration: float, ramp_seconds: float) -> None:
    results = stats.results
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    mask_lat = [r.latency for r in successful if r.op == "mask"]
    demask_lat = [r.latency for r in successful if r.op == "demask"]
    all_lat = [r.latency for r in successful]

    print("\nPrivyGate /process load test (AlfaSonar profile)")
    print("=" * 40)
    print(f"Total requests:       {len(results)}")
    print(f"Successful:           {len(successful)}")
    print(f"Failed:               {len(failed)}")
    print(f"Rate-limited (429):   {stats.rate_limited}")
    print(f"Total duration:       {total_duration:.1f} s")
    print(f"Requests/sec:         {len(results) / total_duration:.2f}")

    if stats.phase_counts:
        ramp_count = stats.phase_counts.get("ramp", 0)
        peak_count = stats.phase_counts.get("peak", 0)
        ramp_secs = min(ramp_seconds, total_duration)
        peak_secs = max(0.0, total_duration - ramp_secs)
        if ramp_secs > 0:
            print(f"Ramp-up requests:     {ramp_count} ({ramp_count / ramp_secs:.1f} RPS)")
        if peak_secs > 0:
            print(f"Peak requests:        {peak_count} ({peak_count / peak_secs:.1f} RPS)")

    def _lat_block(label: str, values: list[float]) -> None:
        if not values:
            print(f"{label}: n/a")
            return
        print(f"{label}:")
        print(f"  mean={statistics.fmean(values):.4f}s "
              f"p50={percentile(values, 0.50):.4f}s "
              f"p95={percentile(values, 0.95):.4f}s "
              f"p99={percentile(values, 0.99):.4f}s")

    _lat_block("Masking latency", mask_lat)
    _lat_block("Demasking latency", demask_lat)
    _lat_block("All latency", all_lat)

    if failed:
        error_counts = Counter(r.error_type or "UnknownError" for r in failed)
        print("Errors:")
        for error_type, count in sorted(error_counts.items()):
            print(f"  {error_type}: {count}")


async def run_load_test(
    url: str,
    target_rps: float,
    peak_rps: float,
    ramp_seconds: float,
    duration: float,
    timeout: float,
    max_retries: int,
    connections: int,
    keepalive: int,
) -> int:
    limiter = RateLimiter(target_rps, peak_rps, ramp_seconds)
    stats = Stats()
    limits = httpx.Limits(
        max_connections=connections,
        max_keepalive_connections=keepalive,
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        limits=limits,
    ) as client:
        # Warm up.
        with contextlib.suppress(httpx.HTTPError):
            await client.post(
                url,
                json={"payload": "Иванов Иван Иванович", "payload_id": "warm"},
                timeout=timeout,
            )

        started_at = time.monotonic()
        counter = 0
        counter_lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal counter
            while time.monotonic() - started_at < duration:
                async with counter_lock:
                    counter += 1
                    request_number = counter
                elapsed = time.monotonic() - started_at
                phase = "ramp" if elapsed < ramp_seconds else "peak"
                stats.phase_counts[phase] = stats.phase_counts.get(phase, 0) + 2
                await execute_pair(
                    client, url, request_number, limiter, stats, max_retries, timeout, phase
                )

        workers = [asyncio.create_task(worker()) for _ in range(connections)]
        await asyncio.gather(*workers, return_exceptions=True)
        total_duration = time.monotonic() - started_at

    print_report(stats, total_duration, ramp_seconds)
    return 0 if not any(not r.success for r in stats.results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rate-controlled load test for /process (AlfaSonar profile).",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"/process URL, default: {DEFAULT_URL}")
    parser.add_argument("--target-rps", type=positive_float, default=DEFAULT_TARGET_RPS,
                        help=f"average RPS during ramp, default: {DEFAULT_TARGET_RPS}")
    parser.add_argument("--peak-rps", type=positive_float, default=DEFAULT_PEAK_RPS,
                        help=f"peak RPS after ramp, default: {DEFAULT_PEAK_RPS}")
    parser.add_argument("--ramp-seconds", type=positive_float, default=DEFAULT_RAMP_SECONDS,
                        help=f"ramp-up duration, default: {DEFAULT_RAMP_SECONDS}")
    parser.add_argument("--duration", type=positive_float, default=DEFAULT_DURATION,
                        help=f"run duration in seconds, default: {DEFAULT_DURATION}")
    parser.add_argument("--timeout", type=positive_float, default=DEFAULT_TIMEOUT,
                        help=f"per-request timeout, default: {DEFAULT_TIMEOUT}")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
                        help=f"retries after first attempt, default: {DEFAULT_MAX_RETRIES}")
    parser.add_argument("--connections", type=int, default=200,
                        help="max concurrent connections, default: 200")
    parser.add_argument("--keepalive", type=int, default=DEFAULT_KEEPALIVE,
                        help=f"max keep-alive connections, default: {DEFAULT_KEEPALIVE}")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(
        run_load_test(
            args.url,
            args.target_rps,
            args.peak_rps,
            args.ramp_seconds,
            args.duration,
            args.timeout,
            args.max_retries,
            args.connections,
            args.keepalive,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
