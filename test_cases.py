"""Eight cases that decide whether the endpoint can be trusted.

FlyRank AI Internship, W6 - Connect to an AI API.

Every message is a real commit from my own repositories except where marked, and every
expected answer is something I can defend from the message text alone. Cases 5 and 6 are
the ones that matter: they are why this endpoint exists instead of a `grep`.

Run:  python test_cases.py           (needs the API on :8100)
"""

from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8100"

CASES = [
    {
        "n": 1,
        "why": "Plain bugfix, stated as one. The easy case.",
        "message": "Fix mobile overflow on the live page",
        "expect_mistake": True,
        "expect_category": {"bugfix"},
    },
    {
        "n": 2,
        "why": "A correction of an earlier commit - an admitted mistake, not a repair.",
        "message": ("Harden the centering, and correct the previous commit message. "
                    "The previous commit said it fixed a mobile overflow. It did not, "
                    "because there was no overflow."),
        "expect_mistake": True,
        "expect_category": {"bugfix", "revert", "refactor", "docs", "chore"},
    },
    {
        "n": 3,
        "why": "Pure feature work. No mistake anywhere in it.",
        "message": "Stage 1: signup and login routes working",
        "expect_mistake": False,
        "expect_category": {"feature", "chore", "unknown"},
    },
    {
        "n": 4,
        "why": "Docs only.",
        "message": "Stage 6: publish to GitHub and write README",
        "expect_mistake": False,
        "expect_category": {"docs", "chore", "feature"},
    },
    {
        "n": 5,
        "why": ("THE FALSE POSITIVE. Contains the literal substring 'fix' inside 'prefix'. "
                "A keyword grep fires; this must not."),
        "message": "Add a prefix to every cache key so tenants cannot read each other's rows",
        "expect_mistake": False,
        "expect_category": {"feature", "refactor", "chore"},
    },
    {
        "n": 6,
        "why": ("THE FALSE NEGATIVE. This is the mojibake encoding bug from the scraper - "
                "a real mistake - and it contains none of the usual keywords. A grep "
                "misses it; this must catch it."),
        "message": ("Stage 5: 60 clean records collected. Also corrects the first run, "
                    "where the site sends no charset so every currency symbol came out "
                    "as two characters."),
        "expect_mistake": True,
        "expect_category": {"bugfix", "chore", "feature", "refactor"},
    },
    {
        "n": 7,
        "why": "An explicit revert. Different category, still a recorded mistake.",
        "message": 'Revert "Stage 4: auth middleware and logout endpoint"',
        "expect_mistake": True,
        "expect_category": {"revert", "bugfix"},
    },
    {
        "n": 8,
        "why": ("Empty of meaning. The honest answer is low confidence, not a guess "
                "dressed up as one."),
        "message": "wip",
        "expect_mistake": False,
        "expect_category": {"unknown", "chore", "feature", "refactor"},
        "expect_low_confidence": True,
    },
]


def run() -> int:
    passed = failed = 0
    lines: list[str] = []
    lines.append("### W6 Connect to an AI API - eight cases")
    lines.append(f"### endpoint {BASE}/classify")
    lines.append("")

    with httpx.Client(timeout=120) as client:
        health = client.get(f"{BASE}/health").json()
        lines.append(f"model: {health['model']} via {health['base_url']}")
        lines.append("")

        for case in CASES:
            started = time.perf_counter()
            r = client.post(f"{BASE}/classify", json={"message": case["message"]})
            elapsed = int((time.perf_counter() - started) * 1000)
            body = r.json()

            problems = []
            if r.status_code != 200:
                problems.append(f"HTTP {r.status_code}")
            else:
                if body["records_mistake"] != case["expect_mistake"]:
                    problems.append(
                        f"records_mistake={body['records_mistake']}, "
                        f"expected {case['expect_mistake']}")
                if body["category"] not in case["expect_category"]:
                    problems.append(
                        f"category={body['category']}, "
                        f"expected one of {sorted(case['expect_category'])}")
                if case.get("expect_low_confidence") and body["confidence"] > 0.75:
                    problems.append(f"confidence={body['confidence']} too high for a vague message")
                if not (0.0 <= body["confidence"] <= 1.0):
                    problems.append(f"confidence out of range: {body['confidence']}")

            ok = not problems
            passed += ok
            failed += not ok

            lines.append(f"--- case {case['n']}: {'PASS' if ok else 'FAIL'} ({elapsed} ms) ---")
            lines.append(f"    why      {case['why']}")
            lines.append(f"    message  {case['message'][:110]}")
            if r.status_code == 200:
                lines.append(f"    got      records_mistake={body['records_mistake']} "
                             f"category={body['category']} confidence={body['confidence']} "
                             f"source={body['source']} attempts={body['attempts']}")
                lines.append(f"    evidence {str(body['evidence'])[:110]}")
            for p in problems:
                lines.append(f"    PROBLEM  {p}")
            lines.append("")

    lines.append("=" * 62)
    lines.append(f"{passed}/{passed + failed} cases passed")
    lines.append("=" * 62)

    report = "\n".join(lines)
    print(report)
    from pathlib import Path
    Path("docs").mkdir(exist_ok=True)
    Path("docs/test-cases.txt").write_text(report, encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
