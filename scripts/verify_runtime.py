#!/usr/bin/env python3
"""Runtime verification script for the Awareness API service.

Queries health, status, jobs, dedup, and tail endpoints to verify correctness.
"""

import sys
import httpx


def test_api():
    base_url = "http://127.0.0.1:8085"
    endpoints = [
        ("/healthz", "Health check"),
        ("/status", "System status"),
        ("/jobs", "List jobs"),
        ("/dedup-stats", "Deduplication index statistics"),
        ("/tail/status", "Tail daemon status"),
    ]

    print("==================================================")
    print("      AWARENESS RUNTIME VERIFICATION SCRIPT       ")
    print("==================================================")
    print(f"Targeting API service at: {base_url}\n")

    all_passed = True

    for endpoint, name in endpoints:
        url = f"{base_url}{endpoint}"
        print(f"Testing {name} ({endpoint})...", end=" ", flush=True)
        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                print("[ PASS ]")
                print(f"  Response: {r.text[:120]}...\n")
            else:
                print(f"[ FAIL ] - Status {r.status_code}")
                print(f"  Response: {r.text}\n")
                all_passed = False
        except Exception as exc:
            print(f"[ ERROR ]")
            print(f"  Exception: {exc}\n")
            all_passed = False

    if all_passed:
        print("==================================================")
        print(" SUCCESS: All endpoints responded correctly!      ")
        print("==================================================")
        sys.exit(0)
    else:
        print("==================================================")
        print(" FAILURE: One or more API checks failed.          ")
        print("==================================================")
        sys.exit(1)


if __name__ == "__main__":
    test_api()
