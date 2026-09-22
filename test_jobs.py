"""Proof for the three non-negotiables of a background job.

FlyRank AI Internship, W7 - BE-06 (Your first background job).

The brief names them: jobs will run twice (idempotency), they will fail (retries), and
someone must find out (alerts). Each gets a test that could fail.

Run:  python test_jobs.py     (needs the API on :8100)
"""

from __future__ import annotations

import concurrent.futures
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8100"
MESSAGES = [
    "Fix mobile overflow on the live page",
    "Add a prefix to every cache key so tenants cannot read each other's rows",
    'Revert "Stage 4: auth middleware and logout endpoint"',
]

lines: list[str] = []


def say(s: str = "") -> None:
    print(s, flush=True)
    lines.append(s)


def wait_for(client: httpx.Client, job_id: str, timeout: float = 600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"{BASE}/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(3)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


def main() -> int:
    passed = failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        passed += ok
        failed += not ok
        say(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if detail:
            say(f"        {detail}")

    say("### W7 BE-06 - background job proof")
    say(f"### {BASE}")
    say("")

    with httpx.Client(timeout=60) as client:
        # ---------------------------------------------------- accept fast
        say("== 1. the endpoint answers immediately, before any work ==")
        started = time.perf_counter()
        r = client.post(f"{BASE}/jobs", json={"messages": MESSAGES},
                        headers={"Idempotency-Key": "proof-run-1"})
        accept_ms = int((time.perf_counter() - started) * 1000)
        body = r.json()
        say(f"  POST /jobs -> HTTP {r.status_code} in {accept_ms} ms")
        say(f"  body: {body}")
        say(f"  Location header: {r.headers.get('Location')}")
        check("returns 202", r.status_code == 202)
        check("answers in well under a second", accept_ms < 1000, f"{accept_ms} ms")
        check("returns a status URL", bool(body.get("status_url")))
        job_id = body["job_id"]
        say("")

        # ------------------------------------------------- idempotency
        say("== 2. idempotency: the same key must not start a second job ==")
        again = client.post(f"{BASE}/jobs", json={"messages": MESSAGES},
                            headers={"Idempotency-Key": "proof-run-1"}).json()
        say(f"  same key -> job_id {again['job_id']} reused={again['reused']}")
        check("same key returns the same job", again["job_id"] == job_id)
        check("and is marked reused", again["reused"] is True)

        # The real test is concurrent, because check-then-insert passes the serial test
        # and loses the race.
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [
                pool.submit(
                    lambda: httpx.post(f"{BASE}/jobs", json={"messages": MESSAGES},
                                       headers={"Idempotency-Key": "proof-race"},
                                       timeout=60).json()["job_id"]
                )
                for _ in range(6)
            ]
            ids = {f.result() for f in futures}
        say(f"  6 concurrent POSTs with one key -> {len(ids)} distinct job id(s)")
        check("6 concurrent requests create exactly one job", len(ids) == 1, f"ids={ids}")

        different = client.post(f"{BASE}/jobs", json={"messages": MESSAGES[:1]},
                                headers={"Idempotency-Key": "proof-other"}).json()
        check("a different key does create a new job", different["job_id"] != job_id)
        say("")

        # ------------------------------------------------ work happens
        say("== 3. the worker does the work after the response ==")
        job = wait_for(client, job_id)
        say(f"  status {job['status']}  progress {job['progress']}  failed {job['failed']}")
        for item in job["items"]:
            v = item["verdict"] or {}
            say(f"    {item['status']:<7} attempts={item['attempts']} "
                f"mistake={v.get('records_mistake')} cat={v.get('category')} "
                f"src={v.get('source')} :: {item['message'][:52]}")
        check("job finished", job["status"] in ("succeeded", "failed"))
        check("every item was processed", job["done"] + job["failed"] == job["total"])
        check("results are attached to the job",
              all(i["verdict"] or i["error"] for i in job["items"]))
        say("")

        # -------------------------------------------- 404 on unknown id
        say("== 4. unknown job id ==")
        missing = client.get(f"{BASE}/jobs/does-not-exist")
        say(f"  GET /jobs/does-not-exist -> {missing.status_code} {missing.text.strip()[:70]}")
        check("unknown id is 404", missing.status_code == 404)
        say("")

    say("=" * 62)
    say(f"{passed}/{passed + failed} checks passed")
    say("=" * 62)

    Path("docs").mkdir(exist_ok=True)
    Path("docs/job-proof.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
