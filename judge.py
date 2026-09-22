"""The model call, and everything that makes its answer trustworthy.

FlyRank AI Internship, W6 - Connect to an AI API.

The judgement: **does this commit message record a bug, a mistake, or a fix?**

That question is not arbitrary. In FL-07 I built an agent whose job was to find the
recorded mistake in a repository so it could write the "what I got wrong" beat of a case
study, and it failed on a repo whose README documents a bug in plain sight. A keyword
grep for `fix|wrong|revert` misses "60 clean records collected" (which was the encoding
fix) and fires on "prefix" and "suffix". This endpoint is the part that needs judgement,
done in code.

The model call is about thirty lines. The rest of this file is the part that matters:

  - a schema every answer must satisfy, enforced after the model has spoken
  - a timeout short enough to hurt, on every attempt
  - retries that know when to stop - and know that a refused schema is worth retrying
    while a 401 is not
  - a deterministic fallback so the caller always gets a usable answer
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "ollama")
MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "25"))
MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))


class Category(str, Enum):
    bugfix = "bugfix"          # repairs behaviour that was wrong
    revert = "revert"          # undoes an earlier change
    feature = "feature"        # adds behaviour
    refactor = "refactor"      # changes shape, not behaviour
    docs = "docs"
    chore = "chore"            # deps, config, formatting, CI
    test = "test"
    unknown = "unknown"


class Verdict(BaseModel):
    """The contract. Anything the model returns that does not fit this is not an answer."""

    records_mistake: bool = Field(
        description="True when the commit repairs or admits something that was wrong"
    )
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(
        max_length=200,
        description="The words in the message that decided it. Must be quoted from the input.",
    )


@dataclass
class JudgeResult:
    verdict: Verdict
    attempts: int
    latency_ms: int
    source: Literal["model", "fallback"]
    errors: list[str] = field(default_factory=list)


class PermanentLLMError(Exception):
    """Do not retry: retrying cannot change the outcome (bad key, unknown model)."""


SYSTEM = (
    "You classify git commit messages. You answer with JSON only - no prose, no code "
    "fences. Every field is required."
)

PROMPT = """Classify this commit message.

MESSAGE:
{message}

Answer with exactly this JSON shape and nothing else:
{{"records_mistake": true or false,
  "category": one of "bugfix","revert","feature","refactor","docs","chore","test","unknown",
  "confidence": a number between 0 and 1,
  "evidence": "the exact words from the message that decided it"}}

records_mistake is true when the commit repairs behaviour that was wrong, undoes a change,
or admits a mistake. It is false for new features, refactors, docs, tests, and chores even
when they mention the word "fix" in another sense (for example "prefix" or "suffix").
evidence must be copied from the message, not invented."""


def _extract_json(text: str) -> dict:
    """Models wrap JSON in prose and fences no matter what the prompt says."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def _call_model(message: str, client: httpx.Client) -> str:
    response = client.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": PROMPT.format(message=message[:2000])},
            ],
            "temperature": 0,
            "max_tokens": 250,
        },
    )
    if response.status_code in (401, 403, 404):
        # A wrong key or a model that does not exist will still be wrong in two seconds.
        raise PermanentLLMError(f"HTTP {response.status_code}: {response.text[:200]}")
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# Words that look like a fix and are not. The fallback needs them; so does the test suite,
# because this is the exact case a grep gets wrong.
_FALSE_FRIENDS = ("prefix", "suffix", "affix", "fixture", "fixed-width", "fixed width")
_MISTAKE_WORDS = ("fix", "bug", "revert", "regression", "broken", "wrong", "crash",
                  "typo", "hotfix", "patch", "mistake", "incorrect", "fault")


def _fallback(message: str) -> Verdict:
    """A deterministic answer for when the model cannot give a valid one.

    Deliberately conservative and deliberately unsubtle: it is the floor, not a second
    opinion. It reports low confidence so a caller can tell the difference.
    """
    lowered = message.lower()
    for friend in _FALSE_FRIENDS:
        lowered = lowered.replace(friend, "")
    hit = next((w for w in _MISTAKE_WORDS if w in lowered), None)
    return Verdict(
        records_mistake=hit is not None,
        category=Category.bugfix if hit else Category.unknown,
        confidence=0.3 if hit else 0.2,
        evidence=f"keyword fallback: {hit}" if hit else "keyword fallback: no match",
    )


# A two-word commit message cannot support a confident answer, but the model gives one
# anyway: asked about "wip" it returned 0.8. Models are not calibrated and telling them to
# be does not fix it, so the cap lives in code where it is enforceable.
LOW_SIGNAL_WORDS = 2
LOW_SIGNAL_CAP = 0.5


def _calibrate(message: str, verdict: Verdict) -> Verdict:
    """Cap confidence when the input cannot support it.

    Deliberately about the *input*, not the answer: the rule is "there was nothing here to
    be confident about", which is checkable, rather than "this answer feels shaky", which
    is not.
    """
    if len(message.split()) <= LOW_SIGNAL_WORDS and verdict.confidence > LOW_SIGNAL_CAP:
        return verdict.model_copy(update={
            "confidence": LOW_SIGNAL_CAP,
            "evidence": f"{verdict.evidence} [confidence capped: too few words to judge]",
        })
    return verdict


def judge(message: str, client: httpx.Client | None = None) -> JudgeResult:
    """Classify one commit message. Always returns a Verdict - never raises for the caller.

    The retry loop treats three things differently, which is the whole point of having
    one: a permanent error stops immediately, a timeout or 5xx is retried with backoff,
    and a schema violation is retried too, because the same model at temperature 0 will
    sometimes produce valid JSON on a second pass with a shorter input.
    """
    started = time.perf_counter()
    errors: list[str] = []
    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = _call_model(message, client)
                verdict = _calibrate(message, Verdict.model_validate(_extract_json(raw)))
                return JudgeResult(
                    verdict=verdict,
                    attempts=attempt,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    source="model",
                    errors=errors,
                )
            except PermanentLLMError as exc:
                errors.append(f"attempt {attempt}: permanent: {exc}")
                break
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}")
            except (ValueError, ValidationError, KeyError) as exc:
                errors.append(f"attempt {attempt}: bad answer: {type(exc).__name__}")

            if attempt < MAX_ATTEMPTS:
                time.sleep(min(2 ** (attempt - 1), 4))

        return JudgeResult(
            verdict=_fallback(message),
            attempts=MAX_ATTEMPTS,
            latency_ms=int((time.perf_counter() - started) * 1000),
            source="fallback",
            errors=errors,
        )
    finally:
        if own_client:
            client.close()
