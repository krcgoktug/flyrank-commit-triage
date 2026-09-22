"""Commit Triage API — one AI judgement, and a background job that does it in bulk.

FlyRank AI Internship, Backend AI Engineering:
  W6  Connect to an AI API      -> POST /classify   (synchronous, schema-checked)
  W7  Your first background job -> POST /jobs       (202 + worker + status)

Same error contract as the rest of my services: every failure is {"error": "..."}.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, FastAPI, Header, HTTPException, Response, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import jobs  # noqa: E402
import judge  # noqa: E402

app = FastAPI(
    title="Commit Triage API",
    version="1.0",
    description=(
        "Asks a model one question — *does this commit message record a mistake?* — and "
        "returns an answer the caller can trust: schema-checked, timed out, retried, with "
        "a deterministic fallback.\n\n"
        "`POST /classify` does one message synchronously. `POST /jobs` takes many, answers "
        "**202** immediately, and a worker does the rest."
    ),
)


@app.on_event("startup")
def _startup() -> None:
    jobs.start_workers(n=1)


@app.on_event("shutdown")
def _shutdown() -> None:
    jobs.stop_workers()


@app.exception_handler(HTTPException)
async def http_error(_r, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_error(_r, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body") or "body"
    return JSONResponse(
        status_code=400,
        content={"error": f"Invalid request body: {field} {first.get('msg', 'is invalid')}"},
    )


@app.exception_handler(Exception)
async def unhandled(_r, _exc):
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# ------------------------------------------------------------------- models
class ClassifyIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000,
                         examples=["fix: currency symbol was mojibake, site sends no charset"])


class JobIn(BaseModel):
    messages: list[str] = Field(min_length=1, max_length=500)


# ------------------------------------------------------------------- routes
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "model": judge.MODEL, "base_url": judge.BASE_URL}


@app.post("/classify", tags=["judgement"], summary="Classify one commit message")
def classify(body: ClassifyIn = Body(...)):
    """Synchronous. Always answers — a model failure degrades to the keyword fallback.

    `source` tells the caller which happened, so a low-confidence fallback is never
    mistaken for the model agreeing.
    """
    result = judge.judge(body.message)
    return {
        **result.verdict.model_dump(mode="json"),
        "source": result.source,
        "attempts": result.attempts,
        "latency_ms": result.latency_ms,
        "errors": result.errors,
    }


@app.post("/jobs", tags=["background"], status_code=status.HTTP_202_ACCEPTED,
          summary="Queue many messages (202)")
def create_job(
    response: Response,
    body: JobIn = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Answers immediately with 202 and an id. Nothing is classified in this request.

    Send the same `Idempotency-Key` twice and you get the same job back, not a second one
    — which is what makes a client retry safe.
    """
    created = jobs.create_job(body.messages, idempotency_key)
    response.headers["Location"] = f"/jobs/{created.job_id}"
    return {
        "job_id": created.job_id,
        "status": "queued",
        "total": len(body.messages),
        "reused": created.reused,
        "status_url": f"/jobs/{created.job_id}",
    }


@app.get("/jobs/{job_id}", tags=["background"], summary="Job status and results")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.get("/alerts", tags=["background"], summary="Every alert raised so far")
def alerts():
    with jobs.connect() as conn:
        rows = conn.execute(
            "SELECT job_id, level, message, created_at FROM alerts ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return {"alerts": [dict(r) for r in rows]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8100")))
