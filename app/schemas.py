from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class AnalyseRequest(BaseModel):
    standard: str  # "frs105" or "frs102"


class AnalyseResponse(BaseModel):
    run_id: str
    status: str


class ProgressEvent(BaseModel):
    stage: str    # extract|redact|assess|review|complete|error
    detail: str
    pct: int


class ChecklistResultOut(BaseModel):
    item_id: str
    requirement: Optional[str]  # single XLS row (may be a short fragment)
    requirement_full: Optional[str] = None  # ancestors + leaf; same text as LLM sees
    status: str
    evidence: Optional[str]
    evidence_location: Optional[str] = None
    evidence_snippet: Optional[str] = None
    reasoning: Optional[str]
    confidence: Optional[float]
    reviewer_changed: int = 0
    human_override: Optional[str]
    human_notes: Optional[str]


class SummaryOut(BaseModel):
    total: int
    met: int
    partially_met: int
    missing: int
    not_applicable: int


class RunOut(BaseModel):
    run_id: str
    filename: str
    standard: str
    status: str
    created_at: datetime
    summary: Optional[SummaryOut]


class ResultsOut(BaseModel):
    run_id: str
    filename: str
    standard: str
    status: str
    created_at: datetime
    summary: SummaryOut
    metadata: Optional[dict[str, Any]]
    items: list[ChecklistResultOut]
