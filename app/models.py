import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=datetime.utcnow)
    filename = Column(String, nullable=False)
    standard = Column(String, nullable=False)  # "frs105" or "frs102"
    status = Column(String, default="pending")  # pending|processing|complete|error
    total_items = Column(Integer)
    met_count = Column(Integer)
    partially_met_count = Column(Integer)
    missing_count = Column(Integer)
    not_applicable_count = Column(Integer)
    error_message = Column(String, nullable=True)
    metadata_ = Column("metadata", JSON)

    items = relationship("ChecklistResult", back_populates="run", cascade="all, delete-orphan")


class ChecklistResult(Base):
    __tablename__ = "checklist_results"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String, ForeignKey("analysis_runs.id"))
    item_id = Column(String, nullable=False)   # e.g. "1.01" or "1.01.a"
    requirement = Column(String)
    status = Column(String)  # met|partially_met|missing|not_applicable
    evidence = Column(String)
    evidence_location = Column(String, nullable=True)
    evidence_snippet = Column(Text, nullable=True)
    reasoning = Column(String)
    confidence = Column(Float)
    reviewer_changed = Column(Integer, default=0)
    human_override = Column(String, nullable=True)
    human_notes = Column(String, nullable=True)

    run = relationship("AnalysisRun", back_populates="items")
