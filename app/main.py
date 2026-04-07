"""
Sparkz API — FastAPI application
"""

import asyncio
import csv
import io
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.checklists.loader import load_checklist
from app.checklists.requirement_context import compose_requirement_context
from app.config import settings
from app.models import AnalysisRun, Base, ChecklistResult
from app.pipeline.orchestrator import get_progress, run_pipeline
from app.schemas import (
    AnalyseResponse,
    ChecklistResultOut,
    ResultsOut,
    RunOut,
    SummaryOut,
)

# ── Database setup ─────────────────────────────────────────────────────────

os.makedirs("./data", exist_ok=True)
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_sqlite_columns() -> None:
    """Add new columns when upgrading an existing SQLite file (create_all does not alter)."""
    if not str(settings.DATABASE_URL).startswith("sqlite"):
        return
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(checklist_results)")).fetchall()
        col_names = {row[1] for row in rows}
        if "evidence_location" not in col_names:
            conn.execute(
                text("ALTER TABLE checklist_results ADD COLUMN evidence_location VARCHAR")
            )
        if "evidence_snippet" not in col_names:
            conn.execute(
                text("ALTER TABLE checklist_results ADD COLUMN evidence_snippet TEXT")
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
    yield


# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Sparkz API", version="2.0.0", lifespan=lifespan)


def _cors_allow_origins() -> list[str]:
    defaults = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
    ]
    extra = [
        o.strip().rstrip("/")
        for o in (settings.CORS_ALLOWED_ORIGINS or "").split(",")
        if o.strip()
    ]
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for o in defaults + extra:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _serialise_result(
    item: ChecklistResult,
    checklist: dict | None = None,
) -> ChecklistResultOut:
    req_full = None
    if checklist is not None:
        req_full = compose_requirement_context(checklist, item.item_id)
    return ChecklistResultOut(
        item_id=item.item_id,
        requirement=item.requirement,
        requirement_full=req_full,
        status=item.status,
        evidence=item.evidence,
        evidence_location=item.evidence_location,
        evidence_snippet=item.evidence_snippet,
        reasoning=item.reasoning,
        confidence=item.confidence,
        reviewer_changed=item.reviewer_changed or 0,
        human_override=item.human_override,
        human_notes=item.human_notes,
    )


def _serialise_run(run: AnalysisRun) -> RunOut:
    summary = None
    if run.total_items is not None:
        summary = SummaryOut(
            total=run.total_items or 0,
            met=run.met_count or 0,
            partially_met=run.partially_met_count or 0,
            missing=run.missing_count or 0,
            not_applicable=run.not_applicable_count or 0,
        )
    return RunOut(
        run_id=run.id,
        filename=run.filename,
        standard=run.standard,
        status=run.status,
        created_at=run.created_at,
        summary=summary,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Sparkz API", "version": "2.0.0"}


@app.post("/api/analyse", response_model=AnalyseResponse)
async def start_analysis(
    file: UploadFile = File(...),
    standard: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Accept a PDF upload and a standard selection ("frs105" or "frs102").
    Returns a run_id immediately. The pipeline runs in the background.
    """
    standard = standard.lower().strip()
    if standard not in ("frs105", "frs102"):
        raise HTTPException(status_code=422, detail="standard must be 'frs105' or 'frs102'")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF files are accepted")

    # Save uploaded file
    run_id = str(uuid.uuid4())
    file_path = Path(settings.UPLOAD_DIR) / f"{run_id}.pdf"
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Create DB record
    run = AnalysisRun(id=run_id, filename=file.filename, standard=standard, status="processing")
    db.add(run)
    db.commit()

    # Start pipeline in background (new DB session — the background task is long-lived)
    bg_db = SessionLocal()

    async def _run_and_close():
        try:
            await run_pipeline(run_id, str(file_path), standard, bg_db)
        finally:
            bg_db.close()

    asyncio.create_task(_run_and_close())

    return AnalyseResponse(run_id=run_id, status="processing")


@app.get("/api/analyse/{run_id}/progress")
async def stream_progress(run_id: str):
    """
    Server-Sent Events stream for real-time pipeline progress.
    Clients connect here after receiving run_id from /api/analyse.
    """
    async def event_generator():
        while True:
            progress = get_progress(run_id)
            yield f"data: {json.dumps(progress)}\n\n"
            if progress["stage"] in ("complete", "error"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/results/{run_id}", response_model=ResultsOut)
def get_results(run_id: str, db: Session = Depends(get_db)):
    """Return full results for a completed analysis run."""
    run = db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    items = db.query(ChecklistResult).filter(ChecklistResult.run_id == run_id).all()

    try:
        checklist = load_checklist(run.standard)
    except Exception:
        checklist = None

    summary = SummaryOut(
        total=run.total_items or len(items),
        met=run.met_count or 0,
        partially_met=run.partially_met_count or 0,
        missing=run.missing_count or 0,
        not_applicable=run.not_applicable_count or 0,
    )

    return ResultsOut(
        run_id=run.id,
        filename=run.filename,
        standard=run.standard,
        status=run.status,
        created_at=run.created_at,
        summary=summary,
        metadata=run.metadata_,
        items=[_serialise_result(item, checklist) for item in items],
    )


@app.patch("/api/results/{run_id}/items/{item_id}")
def update_item(
    run_id: str,
    item_id: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Human override: update status and/or notes for a checklist item."""
    item = (
        db.query(ChecklistResult)
        .filter(ChecklistResult.run_id == run_id, ChecklistResult.item_id == item_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if "human_override" in body:
        item.human_override = body["human_override"]
    if "human_notes" in body:
        item.human_notes = body["human_notes"]

    db.commit()
    return {"ok": True}


@app.get("/api/results/{run_id}/export")
def export_results(run_id: str, format: str = "csv", db: Session = Depends(get_db)):
    """Export results as CSV."""
    run = db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    items = db.query(ChecklistResult).filter(ChecklistResult.run_id == run_id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "item_id", "requirement", "requirement_full", "ai_status", "human_override",
        "final_status", "evidence", "evidence_location", "evidence_snippet",
        "reasoning", "confidence", "reviewer_changed",
    ])
    try:
        checklist = load_checklist(run.standard)
    except Exception:
        checklist = None
    for item in items:
        final_status = item.human_override or item.status
        req_full = (
            compose_requirement_context(checklist, item.item_id)
            if checklist is not None
            else ""
        )
        writer.writerow([
            item.item_id,
            item.requirement or "",
            req_full.replace("\r\n", "\n"),
            item.status or "",
            item.human_override or "",
            final_status or "",
            item.evidence or "",
            item.evidence_location or "",
            (item.evidence_snippet or "").replace("\r\n", "\n"),
            item.reasoning or "",
            f"{item.confidence:.2f}" if item.confidence is not None else "",
            item.reviewer_changed or 0,
        ])

    output.seek(0)
    filename = f"sparkz_{run.standard}_{run_id[:8]}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/runs")
def list_runs(db: Session = Depends(get_db)):
    """List all past analysis runs, most recent first."""
    runs = db.query(AnalysisRun).order_by(AnalysisRun.created_at.desc()).all()
    return [_serialise_run(r) for r in runs]
