"""Query, render, store, link — the classic background report.

FlyRank AI Internship, W7 - PDF report generator.

Three things the brief asks for, in order:

  QUERY     real SQL aggregation over the triage results, not a loop in Python
  RENDER    a PDF on disk
  ARTIFACT  store it and hand back a link. A 20 MB body in a JSON response is how you
            take down a client; the response carries a URL and the bytes are fetched
            once, on demand.

It reuses the BE-06 job machinery rather than growing a second one: same table, same
worker, same 202. The only new thing is a `kind` column, so a job knows whether it is
classifying commits or building a report.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

import jobs

ARTIFACT_DIR = Path("artifacts")


# --------------------------------------------------------------------- query
def aggregate(conn: sqlite3.Connection) -> dict:
    """One pass of SQL, not a loop in Python.

    json_extract lets the verdict stay a single JSON column while still being
    group-by-able, which is the right trade at this size: the shape of a verdict is the
    model's contract, not the database's.
    """
    totals = conn.execute(
        """
        SELECT COUNT(*)                                             AS items,
               SUM(status = 'done')                                 AS done,
               SUM(status = 'failed')                               AS failed
        FROM items
        """
    ).fetchone()

    by_category = conn.execute(
        """
        SELECT json_extract(verdict, '$.category')          AS category,
               COUNT(*)                                     AS n,
               ROUND(AVG(json_extract(verdict, '$.confidence')), 2) AS avg_confidence
        FROM items
        WHERE status = 'done' AND verdict IS NOT NULL
        GROUP BY category
        ORDER BY n DESC
        """
    ).fetchall()

    mistakes = conn.execute(
        """
        SELECT SUM(json_extract(verdict, '$.records_mistake') = 1) AS recorded,
               SUM(json_extract(verdict, '$.records_mistake') = 0) AS clean
        FROM items
        WHERE status = 'done' AND verdict IS NOT NULL
        """
    ).fetchone()

    by_source = conn.execute(
        """
        SELECT json_extract(verdict, '$.source') AS source, COUNT(*) AS n
        FROM items
        WHERE status = 'done' AND verdict IS NOT NULL
        GROUP BY source
        """
    ).fetchall()

    slowest = conn.execute(
        """
        SELECT message, json_extract(verdict, '$.latency_ms') AS ms
        FROM items
        WHERE status = 'done' AND verdict IS NOT NULL
        ORDER BY ms DESC LIMIT 5
        """
    ).fetchall()

    examples = conn.execute(
        """
        SELECT message,
               json_extract(verdict, '$.category')   AS category,
               json_extract(verdict, '$.evidence')   AS evidence
        FROM items
        WHERE status = 'done'
          AND json_extract(verdict, '$.records_mistake') = 1
        ORDER BY json_extract(verdict, '$.confidence') DESC
        LIMIT 8
        """
    ).fetchall()

    job_counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ).fetchall()

    return {
        "items": totals["items"] or 0,
        "done": totals["done"] or 0,
        "failed": totals["failed"] or 0,
        "recorded_mistakes": (mistakes["recorded"] or 0) if mistakes else 0,
        "clean": (mistakes["clean"] or 0) if mistakes else 0,
        "by_category": [dict(r) for r in by_category],
        "by_source": [dict(r) for r in by_source],
        "slowest": [dict(r) for r in slowest],
        "examples": [dict(r) for r in examples],
        "jobs": [dict(r) for r in job_counts],
    }


# -------------------------------------------------------------------- render
INK = (18, 22, 28)
MAIN = (31, 75, 110)
ACCENT = (180, 83, 31)
MUTED = (100, 102, 105)


class Report(FPDF):
    def header(self) -> None:
        self.set_font("helvetica", "B", 9)
        self.set_text_color(*MUTED)
        self.cell(0, 6, "Commit Triage - run report", align="L")
        self.cell(0, 6, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                  align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*MUTED)
        self.line(10, 18, 200, 18)
        self.ln(6)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 8, f"page {self.page_no()}/{{nb}}", align="C")

    def h1(self, text: str) -> None:
        self.set_font("helvetica", "B", 17)
        self.set_text_color(*INK)
        self.multi_cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def h2(self, text: str) -> None:
        self.ln(3)
        self.set_font("helvetica", "B", 11)
        self.set_text_color(*MAIN)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")

    def body(self, text: str) -> None:
        self.set_font("helvetica", "", 9.5)
        self.set_text_color(*INK)
        self.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")

    def kv_row(self, label: str, value: str) -> None:
        self.set_font("helvetica", "", 9.5)
        self.set_text_color(*MUTED)
        self.cell(70, 6, label)
        self.set_text_color(*INK)
        self.set_font("helvetica", "B", 9.5)
        self.cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
        self.set_font("helvetica", "B", 8.5)
        self.set_text_color(255, 255, 255)
        self.set_fill_color(*MAIN)
        for h, w in zip(headers, widths):
            self.cell(w, 7, h, fill=True)
        self.ln()
        self.set_text_color(*INK)
        self.set_font("helvetica", "", 8.5)
        fill = False
        for row in rows:
            self.set_fill_color(245, 245, 242) if fill else self.set_fill_color(255, 255, 255)
            for cell, w in zip(row, widths):
                self.cell(w, 6, str(cell)[: int(w / 1.7)], fill=True)
            self.ln()
            fill = not fill


def _ascii(text: str) -> str:
    """The core PDF fonts are latin-1 only; this keeps a stray em dash from raising."""
    return (text or "").encode("latin-1", "replace").decode("latin-1")


def render(data: dict, out_path: Path) -> Path:
    pdf = Report()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    pdf.h1("Commit triage: what the model found")
    pdf.body(_ascii(
        "Generated from the triage database by a background job. Every number below is a "
        "SQL aggregate over stored verdicts - nothing here is recomputed by hand, and "
        "nothing is estimated."
    ))

    pdf.h2("Totals")
    # "items" is every row in the table, which includes anything still pending from a
    # job that died. Labelling that "classified" would overstate the work done.
    pdf.kv_row("Commit messages in the database", str(data["items"]))
    pdf.kv_row("Classified successfully", str(data["done"]))
    pdf.kv_row("Failed after retries", str(data["failed"]))
    pending = data["items"] - data["done"] - data["failed"]
    if pending:
        pdf.kv_row("Still pending (from an interrupted job)", str(pending))
    pdf.kv_row("Recorded a mistake", str(data["recorded_mistakes"]))
    pdf.kv_row("Clean (no mistake)", str(data["clean"]))

    if data["by_category"]:
        pdf.h2("By category")
        pdf.table(
            ["Category", "Count", "Avg confidence"],
            [[_ascii(str(r["category"])), r["n"], r["avg_confidence"]] for r in data["by_category"]],
            [70, 30, 40],
        )

    if data["by_source"]:
        pdf.h2("Model vs fallback")
        pdf.body(_ascii(
            "A verdict from the keyword fallback means the model could not produce a valid "
            "answer in three attempts. A high fallback count is a provider problem, not a "
            "data problem."
        ))
        pdf.table(["Source", "Count"],
                  [[_ascii(str(r["source"])), r["n"]] for r in data["by_source"]], [70, 30])

    if data["examples"]:
        pdf.h2("Commits that record a mistake")
        pdf.table(
            ["Message", "Category", "Evidence"],
            [[_ascii(r["message"]), _ascii(str(r["category"])), _ascii(str(r["evidence"]))]
             for r in data["examples"]],
            [78, 26, 76],
        )

    if data["slowest"]:
        pdf.h2("Slowest calls")
        pdf.table(["Message", "ms"],
                  [[_ascii(r["message"]), r["ms"]] for r in data["slowest"]], [150, 30])

    pdf.h2("Jobs")
    pdf.table(["Status", "Count"],
              [[_ascii(str(r["status"])), r["n"]] for r in data["jobs"]], [70, 30])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


# ---------------------------------------------------------------------- job
def build_report(job_id: str) -> Path:
    """Run the query and render the artifact. Called by the worker, never by a request."""
    with jobs.connect() as conn:
        data = aggregate(conn)
    return render(data, ARTIFACT_DIR / f"{job_id}.pdf")
