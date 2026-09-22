"""The job store and the worker.

FlyRank AI Internship, W7 - BE-06 (Your first background job).

Classifying one commit takes a couple of seconds. Classifying a repository's history takes
minutes, and a request that takes minutes is not a request - the client times out, a retry
starts the whole thing again, and nobody can tell you how far it got.

So: `POST /jobs` returns **202** with an id, a worker does the work, `GET /jobs/{id}`
reports progress. The three non-negotiables the brief names are the three hard parts:

  IDEMPOTENCY  the same Idempotency-Key returns the same job instead of starting a second
               one. Enforced by a UNIQUE index, not by a check-then-insert, because
               check-then-insert is a race with two workers.
  RETRIES      a failed item is retried with backoff up to MAX_ITEM_ATTEMPTS, then the
               item is marked failed and the job carries on. One bad commit must not kill
               a run of four hundred.
  ALERTS       a job that ends with failures writes an alert row and a line to
               alerts.log. A failure nobody hears about is the same as a silent success,
               which is worse than a crash.

SQLite with WAL is the store. It is the right size for this: the work is minutes-long and
single-machine, and a job queue that needs no server is a job queue that actually runs.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

import judge

# Fault injection, off unless the env var is set. It exists so the retry and alert paths
# can be proven rather than asserted: judge() degrades to a keyword fallback instead of
# raising, so on the happy path an item never fails and an alert never fires. Without a
# way to force a failure, "someone must find out" would be untested code.
FAULT_SUBSTRING = os.environ.get("TRIAGE_FAULT_SUBSTRING")

DB_PATH = Path("jobs.db")
ALERT_LOG = Path("alerts.log")
MAX_ITEM_ATTEMPTS = 3

_work_queue: "queue.Queue[str]" = queue.Queue()
_workers: list[threading.Thread] = []
_stop = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL so the worker writing progress does not block the status endpoint reading it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id              TEXT PRIMARY KEY,
                idempotency_key TEXT,
                status          TEXT NOT NULL,   -- queued|running|succeeded|failed
                total           INTEGER NOT NULL DEFAULT 0,
                done            INTEGER NOT NULL DEFAULT 0,
                failed          INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                started_at      TEXT,
                finished_at     TEXT,
                error           TEXT
            );

            -- The idempotency guarantee lives here, in the schema. Two concurrent POSTs
            -- with the same key cannot both insert; the loser gets an IntegrityError and
            -- returns the winner's job.
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_idem
                ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;

            CREATE TABLE IF NOT EXISTS items (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id    TEXT NOT NULL REFERENCES jobs(id),
                message   TEXT NOT NULL,
                status    TEXT NOT NULL,          -- pending|done|failed
                attempts  INTEGER NOT NULL DEFAULT 0,
                verdict   TEXT,
                error     TEXT
            );
            CREATE INDEX IF NOT EXISTS items_job ON items(job_id);

            CREATE TABLE IF NOT EXISTS alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id     TEXT NOT NULL,
                level      TEXT NOT NULL,
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def raise_alert(job_id: str, level: str, message: str) -> None:
    """Two destinations on purpose: one queryable, one a human can tail."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO alerts (job_id, level, message, created_at) VALUES (?,?,?,?)",
            (job_id, level, message, _now()),
        )
    with ALERT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{_now()} [{level}] job={job_id} {message}\n")


@dataclass
class CreatedJob:
    job_id: str
    reused: bool


def create_job(messages: list[str], idempotency_key: str | None) -> CreatedJob:
    """Insert a job and its items. Returns the existing job when the key repeats."""
    if idempotency_key:
        with connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row:
                return CreatedJob(row["id"], reused=True)

    job_id = str(uuid.uuid4())
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, idempotency_key, status, total, created_at) "
                "VALUES (?,?,?,?,?)",
                (job_id, idempotency_key, "queued", len(messages), _now()),
            )
            conn.executemany(
                "INSERT INTO items (job_id, message, status) VALUES (?,?,'pending')",
                [(job_id, m) for m in messages],
            )
    except sqlite3.IntegrityError:
        # Lost the race on the unique index - the other request's job is the real one.
        with connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return CreatedJob(row["id"], reused=True)

    _work_queue.put(job_id)
    return CreatedJob(job_id, reused=False)


def get_job(job_id: str) -> dict | None:
    with connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return None
        items = conn.execute(
            "SELECT message, status, attempts, verdict, error FROM items "
            "WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        alerts = conn.execute(
            "SELECT level, message, created_at FROM alerts WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()

    out = dict(job)
    out["progress"] = f"{job['done'] + job['failed']}/{job['total']}"
    out["items"] = [
        {**dict(i), "verdict": json.loads(i["verdict"]) if i["verdict"] else None}
        for i in items
    ]
    out["alerts"] = [dict(a) for a in alerts]
    return out


def _process(job_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                     (_now(), job_id))
        items = conn.execute(
            "SELECT id, message FROM items WHERE job_id=? AND status='pending' ORDER BY id",
            (job_id,),
        ).fetchall()

    client = httpx.Client(timeout=judge.TIMEOUT)
    done = failed = 0
    try:
        for item in items:
            last_error = None
            for attempt in range(1, MAX_ITEM_ATTEMPTS + 1):
                try:
                    if FAULT_SUBSTRING and FAULT_SUBSTRING in item["message"]:
                        raise RuntimeError(f"injected fault ({FAULT_SUBSTRING})")
                    result = judge.judge(item["message"], client=client)
                    payload = {
                        **result.verdict.model_dump(mode="json"),
                        "source": result.source,
                        "llm_attempts": result.attempts,
                        "latency_ms": result.latency_ms,
                    }
                    with connect() as conn:
                        conn.execute(
                            "UPDATE items SET status='done', attempts=?, verdict=?, error=NULL "
                            "WHERE id=?",
                            (attempt, json.dumps(payload), item["id"]),
                        )
                    done += 1
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 - one item must not kill the job
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < MAX_ITEM_ATTEMPTS:
                        time.sleep(min(2 ** (attempt - 1), 4))

            if last_error is not None:
                failed += 1
                with connect() as conn:
                    conn.execute(
                        "UPDATE items SET status='failed', attempts=?, error=? WHERE id=?",
                        (MAX_ITEM_ATTEMPTS, last_error, item["id"]),
                    )

            with connect() as conn:
                conn.execute("UPDATE jobs SET done=?, failed=? WHERE id=?",
                             (done, failed, job_id))

        status = "succeeded" if failed == 0 else "failed"
        with connect() as conn:
            conn.execute("UPDATE jobs SET status=?, finished_at=? WHERE id=?",
                         (status, _now(), job_id))
        if failed:
            raise_alert(job_id, "error",
                        f"{failed}/{len(items)} items failed after {MAX_ITEM_ATTEMPTS} attempts")
    except Exception as exc:  # noqa: BLE001 - the worker itself fell over
        with connect() as conn:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (_now(), f"{type(exc).__name__}: {exc}", job_id))
        raise_alert(job_id, "critical", f"worker crashed: {type(exc).__name__}: {exc}")
    finally:
        client.close()


def _worker_loop() -> None:
    while not _stop.is_set():
        try:
            job_id = _work_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _process(job_id)
        finally:
            _work_queue.task_done()


def start_workers(n: int = 1) -> None:
    init_db()
    # Requeue anything left running when the process died, so a restart resumes instead
    # of stranding a job at "running" forever.
    with connect() as conn:
        stranded = conn.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()
    for row in stranded:
        _work_queue.put(row["id"])

    for _ in range(n):
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()
        _workers.append(t)


def stop_workers() -> None:
    _stop.set()
