#!/usr/bin/env python3
"""
OpenInterest Lens — Load Test Script

Sends a configurable number of requests to test rate limiting,
response times, and throughput under load.

Usage:
    python scripts/load_test.py --url http://localhost:8000 --key oil_sk_test_xxx --requests 1000 --concurrency 10
"""

import argparse
import asyncio
import statistics
import time
from typing import Any

import httpx


async def make_request(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    endpoint: str,
) -> dict[str, Any]:
    """Make a single request and measure response time."""
    headers = {"X-API-Key": api_key}
    start = time.monotonic()
    try:
        response = await client.get(f"{url}{endpoint}", headers=headers, timeout=30)
        elapsed = time.monotonic() - start
        return {
            "status": response.status_code,
            "elapsed": elapsed,
            "endpoint": endpoint,
            "error": None,
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        return {
            "status": 0,
            "elapsed": elapsed,
            "endpoint": endpoint,
            "error": str(e),
        }


async def run_load_test(
    url: str,
    api_key: str,
    total_requests: int,
    concurrency: int,
    endpoints: list[str],
) -> None:
    """Run the load test with the given parameters."""
    print(f"\n🚀 OpenInterest Lens Load Test")
    print(f"   URL: {url}")
    print(f"   Total requests: {total_requests}")
    print(f"   Concurrency: {concurrency}")
    print(f"   Endpoints: {endpoints}")
    print(f"   Rate: ~{total_requests / (total_requests * 0.01):.0f} req/s target")
    print()

    results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_request(endpoint: str) -> dict[str, Any]:
        async with semaphore:
            return await make_request(client, url, api_key, endpoint)

    async with httpx.AsyncClient() as client:
        start_time = time.monotonic()

        # Distribute requests across endpoints
        tasks = []
        for i in range(total_requests):
            endpoint = endpoints[i % len(endpoints)]
            tasks.append(bounded_request(endpoint))

        # Run in batches
        batch_size = 100
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i + batch_size]
            batch_results = await asyncio.gather(*batch)
            results.extend(batch_results)
            if (i + batch_size) % 500 == 0:
                print(f"   Completed {len(results)}/{total_requests} requests...")

        total_time = time.monotonic() - start_time

    # ── Results ──────────────────────────────────────────────────────────────
    response_times = [r["elapsed"] for r in results if r["status"] > 0]
    status_counts: dict[int, int] = {}
    errors = 0

    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        if r["error"]:
            errors += 1

    print(f"\n{'='*60}")
    print(f"  Load Test Results")
    print(f"{'='*60}")
    print(f"  Total requests:    {total_requests}")
    print(f"  Total time:        {total_time:.2f}s")
    print(f"  Requests/sec:      {total_requests / total_time:.1f}")
    print(f"  Errors:            {errors}")
    print(f"\n  Response Times:")
    if response_times:
        print(f"    Min:   {min(response_times)*1000:.1f}ms")
        print(f"    Max:   {max(response_times)*1000:.1f}ms")
        print(f"    Mean:  {statistics.mean(response_times)*1000:.1f}ms")
        print(f"    Median:{statistics.median(response_times)*1000:.1f}ms")
        if len(response_times) > 1:
            print(f"    P95:   {sorted(response_times)[int(len(response_times)*0.95)]*1000:.1f}ms")
            print(f"    P99:   {sorted(response_times)[int(len(response_times)*0.99)]*1000:.1f}ms")
    print(f"\n  Status Codes:")
    for status, count in sorted(status_counts.items()):
        print(f"    {status}: {count} ({count/total_requests*100:.1f}%)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenInterest Lens Load Test")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--key", default="oil_sk_test_development_key", help="API key")
    parser.add_argument("--requests", type=int, default=100, help="Total number of requests")
    parser.add_argument("--concurrency", type=int, default=10, help="Max concurrent requests")
    parser.add_argument("--endpoints", nargs="+", default=["/v1/health", "/v1/contracts"],
                        help="Endpoints to test (rotated evenly)")
    args = parser.parse_args()

    asyncio.run(run_load_test(args.url, args.key, args.requests, args.concurrency, args.endpoints))


if __name__ == "__main__":
    main()