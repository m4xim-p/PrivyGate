#!/usr/bin/env python3
"""Small asynchronous streaming load test for PrivyGate."""

import argparse
import asyncio
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass

import httpx


DEFAULT_URL = "http://localhost:8000/v1/chat/completions"


@dataclass(frozen=True, slots=True)
class RequestResult:
    success: bool
    ttfb: float | None
    total_latency: float
    error_type: str | None = None


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def percentile(values: list[float], quantile: float) -> float:
    """Calculate a percentile using linear interpolation between observations."""

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


def request_payload(request_number: int) -> dict[str, object]:
    """Create request-specific synthetic PII, never real user data."""

    phone_suffix = request_number % 100
    return {
        "model": "mock-model",
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Load test request {request_number}: позвони на "
                    f"+7 999 000-00-{phone_suffix:02d} или напиши "
                    f"loadtest-{request_number}@example.com"
                ),
            }
        ],
        "stream": True,
    }


async def execute_request(
    client: httpx.AsyncClient,
    url: str,
    request_number: int,
    start_event: asyncio.Event,
) -> RequestResult:
    await start_event.wait()
    started_at = time.perf_counter()
    ttfb: float | None = None

    try:
        async with client.stream(
            "POST",
            url,
            json=request_payload(request_number),
        ) as response:
            # Consume even non-2xx bodies so every opened response is closed cleanly.
            async for chunk in response.aiter_bytes():
                if chunk and ttfb is None:
                    ttfb = time.perf_counter() - started_at

            response.raise_for_status()

        total_latency = time.perf_counter() - started_at
        if ttfb is None:
            return RequestResult(
                success=False,
                ttfb=None,
                total_latency=total_latency,
                error_type="EmptyResponse",
            )

        return RequestResult(
            success=True,
            ttfb=ttfb,
            total_latency=total_latency,
        )
    except Exception as exc:
        return RequestResult(
            success=False,
            ttfb=ttfb,
            total_latency=time.perf_counter() - started_at,
            error_type=type(exc).__name__,
        )


def format_seconds(value: float) -> str:
    return f"{value:.3f} s"


def print_report(results: list[RequestResult], total_duration: float) -> None:
    successful = [result for result in results if result.success]
    failed = [result for result in results if not result.success]
    ttfb_values = [result.ttfb for result in successful if result.ttfb is not None]
    latency_values = [result.total_latency for result in successful]

    print("\nPrivyGate streaming load test")
    print("=" * 32)
    print(f"Total requests:       {len(results)}")
    print(f"Successful:           {len(successful)}")
    print(f"Failed:               {len(failed)}")
    print(f"Total duration:       {format_seconds(total_duration)}")
    print(f"Requests/sec:         {len(results) / total_duration:.2f}")

    if successful:
        print(f"Average TTFB:         {format_seconds(statistics.fmean(ttfb_values))}")
        print(f"p50 TTFB:             {format_seconds(percentile(ttfb_values, 0.50))}")
        print(f"p95 TTFB:             {format_seconds(percentile(ttfb_values, 0.95))}")
        print(
            f"Average total latency:{format_seconds(statistics.fmean(latency_values)):>12}"
        )
        print(
            f"p95 total latency:    {format_seconds(percentile(latency_values, 0.95))}"
        )
    else:
        print("Average TTFB:         n/a")
        print("p50 TTFB:             n/a")
        print("p95 TTFB:             n/a")
        print("Average total latency:n/a")
        print("p95 total latency:    n/a")

    if failed:
        error_counts = Counter(result.error_type or "UnknownError" for result in failed)
        print("Errors:")
        for error_type, count in sorted(error_counts.items()):
            print(f"  {error_type}: {count}")


async def run_load_test(url: str, concurrency: int, timeout: float) -> int:
    start_event = asyncio.Event()
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        limits=limits,
    ) as client:
        tasks = [
            asyncio.create_task(
                execute_request(client, url, request_number, start_event)
            )
            for request_number in range(1, concurrency + 1)
        ]
        started_at = time.perf_counter()
        start_event.set()
        results = await asyncio.gather(*tasks)
        total_duration = time.perf_counter() - started_at

    print_report(results, total_duration)
    return 0 if all(result.success for result in results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open concurrent streaming requests against PrivyGate.",
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int,
        default=10,
        help="number of concurrent requests (and total requests), default: 10",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Gateway chat completions URL, default: {DEFAULT_URL}",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=60.0,
        help="httpx timeout in seconds, default: 60",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_load_test(args.url, args.concurrency, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
