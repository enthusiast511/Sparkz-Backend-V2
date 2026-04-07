"""
Pipeline Orchestrator
=====================
Runs the full pipeline: Extract → Redact → Assess → Review → Persist

Progress is stored in an in-memory dict keyed by run_id so the SSE
endpoint can stream it to the client in real time.
"""

import asyncio
import traceback
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.checklists.loader import flatten_checklist_items, load_checklist
from app.checklists.requirement_context import enrich_items_for_llm
from app.models import AnalysisRun, ChecklistResult
from app.pipeline.assessor import run_assessment
from app.pipeline.entity_applicability import (
    apply_entity_gate_to_assessments,
    apply_entity_gate_to_reviewed,
    detect_entity_type,
)
from app.pipeline.extractor import extract_pdf
from app.pipeline.redactor import redact_document
from app.pipeline.reviewer import ReviewedResult, run_review
from app.utils.openai_client import get_openai_client

# ── In-memory progress store ───────────────────────────────────────────────
_progress: dict[str, dict] = {}


def _update_progress(run_id: str, stage: str, detail: str, pct: int) -> None:
    _progress[run_id] = {"stage": stage, "detail": detail, "pct": pct}


def get_progress(run_id: str) -> dict:
    return _progress.get(run_id, {"stage": "pending", "detail": "Waiting to start", "pct": 0})


# ── Helpers ────────────────────────────────────────────────────────────────

def _compute_summary(results: list[ReviewedResult]) -> dict:
    counts = {"met": 0, "partially_met": 0, "missing": 0, "not_applicable": 0}
    for r in results:
        status = r.final_status
        if status in counts:
            counts[status] += 1
    return counts


def _persist_results(
    db: Session,
    run_id: str,
    checklist_items: list[dict],
    reviewed: list[ReviewedResult],
    metadata: dict,
) -> None:
    """Write results to DB and update the AnalysisRun record."""
    summary = _compute_summary(reviewed)
    reviewed_by_id = {r.item_id: r for r in reviewed}
    item_req_by_id = {item["id"]: item.get("requirement", "") for item in checklist_items}

    for r in reviewed:
        db.add(ChecklistResult(
            run_id=run_id,
            item_id=r.item_id,
            requirement=item_req_by_id.get(r.item_id, ""),
            status=r.final_status,
            evidence=r.evidence,
            evidence_location=r.evidence_location,
            evidence_snippet=r.evidence_snippet,
            reasoning=r.reasoning,
            confidence=r.confidence,
            reviewer_changed=1 if r.changed else 0,
        ))

    run = db.query(AnalysisRun).get(run_id)
    if run:
        run.status = "complete"
        run.total_items = len(reviewed)
        run.met_count = summary["met"]
        run.partially_met_count = summary["partially_met"]
        run.missing_count = summary["missing"]
        run.not_applicable_count = summary["not_applicable"]
        run.metadata_ = metadata

    db.commit()


# ── Main pipeline ──────────────────────────────────────────────────────────

async def run_pipeline(
    run_id: str,
    file_path: str,
    standard: str,
    db: Session,
) -> None:
    """
    Full pipeline: Extract → Redact → Assess → Review → Persist.
    Updates _progress throughout. Stores errors in the AnalysisRun record.
    """
    client = get_openai_client()

    try:
        # 1. Extract
        _update_progress(run_id, "extract", "Parsing PDF...", 0)
        document = await asyncio.get_event_loop().run_in_executor(
            None, extract_pdf, file_path
        )
        _update_progress(run_id, "extract", f"Extracted {document.total_pages} pages", 10)

        # 2. Redact
        _update_progress(run_id, "redact", "Removing PII...", 15)
        redacted_text, redaction_map = await asyncio.get_event_loop().run_in_executor(
            None, redact_document, document
        )
        _update_progress(run_id, "redact", "PII redacted", 20)
        entity_type = detect_entity_type(redacted_text)

        # 3. Load checklist
        checklist = load_checklist(standard)
        items = flatten_checklist_items(checklist)

        # 4. Assess
        _update_progress(run_id, "assess", f"Analysing {len(items)} checklist items...", 22)

        def on_assess_progress(done: int, total: int) -> None:
            pct = 22 + int(48 * done / total)
            _update_progress(run_id, "assess", f"Assessed {done}/{total} items", pct)

        items_llm = enrich_items_for_llm(checklist, items)
        items_for_llm = [
            {
                "id": item["id"],
                "requirement": item.get("requirement", ""),
                "guidance": item.get("guidance", ""),
                "applicability_hint": item.get("applicability_hint", ""),
                "applicability_rules": item.get("applicability_rules", []),
                "entity_type": entity_type,
            }
            for item in items_llm
        ]

        assessments = await run_assessment(
            client, redacted_text, items_for_llm, on_progress=on_assess_progress
        )
        apply_entity_gate_to_assessments(items_llm, assessments, entity_type)

        # 5. Review
        _update_progress(run_id, "review", "Reviewing assessments...", 75)

        def on_review_progress(done: int, total: int) -> None:
            pct = 75 + int(20 * done / total)
            _update_progress(run_id, "review", f"Reviewed {done}/{total} items", pct)

        reviewed = await run_review(
            client,
            redacted_text,
            items_llm,
            assessments,
            entity_type=entity_type,
            on_progress=on_review_progress,
        )
        apply_entity_gate_to_reviewed(items_llm, reviewed, entity_type)

        # 6. Persist
        _update_progress(run_id, "review", "Saving results...", 97)
        metadata = {
            "pages": document.total_pages,
            "token_estimate": document.token_estimate,
            "standard": standard,
            "entity_type": entity_type,
        }
        await asyncio.get_event_loop().run_in_executor(
            None, _persist_results, db, run_id, items, reviewed, metadata
        )

        _update_progress(run_id, "complete", "Analysis complete", 100)

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        _update_progress(run_id, "error", error_msg, 0)

        # Mark run as errored in DB
        try:
            run = db.query(AnalysisRun).get(run_id)
            if run:
                run.status = "error"
                run.error_message = error_msg
                db.commit()
        except Exception:
            pass

        raise
