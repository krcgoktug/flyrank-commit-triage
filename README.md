# Commit Triage API — one AI judgement, and a job queue that survives it

Asks a model one question — **does this commit message record a mistake?** — and returns
an answer the caller can actually trust. Then does it in bulk, in the background.

FlyRank AI Internship, Backend AI Engineering:

| Week | Assignment | What it is here |
| --- | --- | --- |
| W6 | **Connect to an AI API** | `POST /classify` — schema, timeout, bounded retries, fallback, 8 cases |
| W7 | **Your first background job** | `POST /jobs` → **202**, worker, `GET /jobs/{id}` — idempotency, retries, alerts |

FastAPI + httpx + pydantic, against any OpenAI-compatible endpoint. Default is **local
Ollama**: free, no credit card, works offline.

---

## Why this judgement

Not a chatbot, and not a toy. In [FL-07](https://github.com/krcgoktug/flyrank-workflow-agent)
I built an agent that had to find the recorded mistake in a repository so it could write
the "what I got wrong" beat of a case study. It failed on a repo whose README documents a
bug in plain sight.

The keyword approach fails in both directions, and both are in the test suite:

- `Add a **prefix** to every cache key` — a grep for `fix` fires. It is a feature.
- `Stage 5: 60 clean records collected` — no keyword at all. It **is** the encoding fix.

That gap is the judgement, and it is the one step worth a model call.

## Run it

Needs Python 3.10+ and [Ollama](https://ollama.com/download). No API key: the model runs
locally, so this costs nothing and works offline.

```bash
cp .env.example .env
pip install -r requirements.txt

ollama pull qwen2.5:7b             # 4.7 GB, once - the step I had left out
ollama serve                       # or point LLM_BASE_URL at any OpenAI-compatible API

uvicorn main:app --port 8100

python test_cases.py               # W6: the eight cases
python test_jobs.py                # W7: 202, idempotency, worker, 404
```

The first model call takes ~15 s while the weights load; the rest are about a second.

Docs at `/docs`.

## Making the answer trustworthy

The model call is thirty lines. These four are the assignment:

**A schema, enforced after the model has spoken.** `Verdict` is a pydantic model:
`records_mistake` bool, `category` enum, `confidence` 0–1, `evidence` ≤ 200 chars. Anything
that does not validate is not an answer, and triggers a retry.

**A timeout short enough to hurt.** 25s per attempt, on every attempt, not on the whole
call.

**Retries that know when to stop.** Three outcomes, three behaviours: a `401`/`403`/`404` is
permanent and stops immediately (retrying a bad key wastes 25 seconds to learn nothing); a
timeout or 5xx retries with backoff; a schema violation *also* retries, because the same
model at temperature 0 will sometimes return valid JSON on a second pass.

**A fallback, clearly labelled.** When all attempts fail, a deterministic keyword
classifier answers with low confidence, and `source: "fallback"` says so. The caller is
never left guessing whether the model agreed.

## The case that failed, and what it changed

Seven of eight passed on the first run. Case 8 did not:

```
message:  "wip"
got:      records_mistake=false category=unknown confidence=0.8
PROBLEM:  confidence=0.8 too high for a vague message
```

The model is not calibrated. Asked about a two-word message with no information, it
returned 0.8 — and any caller gating on `confidence > 0.75` would have trusted a guess.

Telling a model to be calibrated does not make it calibrated, so the cap lives in code:
`_calibrate()` caps confidence at 0.5 when the input has two words or fewer, and says so
in `evidence`. The rule is about the **input** — "there was nothing here to be confident
about" is checkable; "this answer feels shaky" is not.

After that change: **8/8**. Full output in [`docs/test-cases.txt`](docs/test-cases.txt).

## The background job

Classifying one commit takes seconds; a repository takes minutes. A request that takes
minutes is not a request — the client times out, its retry starts the work again, and
nobody can say how far it got.

```
POST /jobs      -> 202 in 29 ms, with a job id and a Location header
GET  /jobs/{id} -> queued | running | succeeded | failed, with per-item verdicts
GET  /alerts    -> every alert raised
```

**Idempotency lives in the schema, not in a check.** A partial unique index on
`idempotency_key`. Check-then-insert passes a serial test and loses the race — so the test
fires **six concurrent POSTs** with one key and asserts exactly one job exists. It does.

**Retries are per item, and a failure is isolated.** Three attempts with backoff, then the
item is marked failed and the job carries on. One bad commit must not kill a run of four
hundred.

**Alerts go two places.** A row in `alerts` (queryable) and a line in `alerts.log`
(tailable). A failure nobody hears about is a silent success, which is worse than a crash.

Proof in [`docs/job-proof.txt`](docs/job-proof.txt) — 11/11 checks.

## Proving the error path, not just describing it

The happy path succeeds, which means the retry and alert code never ran. Untested error
handling is not error handling, so there is an off-by-default fault injector:
`TRIAGE_FAULT_SUBSTRING`. With it set, one item of three is poisoned:

```
status           : failed
total/done/failed: 3  2  1
  done    attempts=1  Fix mobile overflow on the live page
  failed  attempts=3  RuntimeError: injected fault (POISON)
  done    attempts=1  Stage 6: publish to GitHub and write README

alerts: error - 1/3 items failed after 3 attempts
alerts.log: 2026-09-22T17:22:27+00:00 [error] job=5ba0c7d1… 1/3 items failed after 3 attempts
```

Three retries, the other two items unaffected, the job correctly `failed`, and the alert
in both destinations. Full run in [`docs/alert-proof.txt`](docs/alert-proof.txt).


## The PDF report (W7)

The classic background job: query, render, store, **link**.

```
POST /reports            -> 202 in 116 ms, nothing rendered in the request
GET  /reports/{id}       -> status, and a download_url once it exists
GET  /reports/{id}/file  -> application/pdf, 2,822 bytes
```

The response never carries the bytes. A 20 MB body in JSON is how you take a client down,
so the worker writes the artifact to `artifacts/{job_id}.pdf` and the API hands back a URL.
`410` if the file has since been deleted, which is a different problem from `404`.

The numbers come from **SQL**, not a Python loop — `json_extract` over the stored verdicts
lets the verdict stay one JSON column while still being `GROUP BY`-able:

```sql
SELECT json_extract(verdict, '$.category') AS category,
       COUNT(*) AS n,
       ROUND(AVG(json_extract(verdict, '$.confidence')), 2) AS avg_confidence
FROM items WHERE status = 'done' GROUP BY category ORDER BY n DESC
```

Sample output: [`docs/report-sample.pdf`](docs/report-sample.pdf). Run log:
[`docs/report-proof.txt`](docs/report-proof.txt).

**Two bugs this caught in my own code**, both while building it:

1. `UnboundLocalError: cannot access local variable 'items'`. Adding the report branch to
   the worker left the `items` query *below* an early `return`, so every classify job
   failed 0/6 instantly. The job row recorded the exception, which is the only reason it
   took a minute to find rather than an afternoon.
2. The first report said **"Commit messages classified: 13"** when only 6 had been
   classified — 13 was every row in the table, including items left `pending` by the job
   that crashed in (1). The number was right and the label was a lie, which is the more
   dangerous kind of reporting bug. It now separates "in the database", "classified
   successfully", and "still pending from an interrupted job".

## Still open

- **Latency is 4–24s per call** on a 7B model on CPU. Fine for a background job, too slow
  to put in a request path — which is exactly why the job queue exists.
- **The worker is in-process.** A restart requeues anything stranded in `queued`/`running`,
  but if the machine dies mid-item that item restarts from scratch. A real queue would
  checkpoint.
- **One worker thread.** Raising it needs per-item claiming (`UPDATE … WHERE status='pending'`)
  or two workers will take the same row.
- **`confidence` is the model's own number**, capped for short inputs. It has not been
  validated against a labelled set, so treat it as a sort key, not a probability.
