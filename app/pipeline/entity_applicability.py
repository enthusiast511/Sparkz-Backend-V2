"""
Deterministic entity-type applicability handling.

Ensures company-only / LLP-only rules are enforced consistently regardless of LLM drift.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from app.pipeline.assessor import Assessment
from app.pipeline.reviewer import ReviewedResult

EntityType = Literal["company", "llp", "unknown"]


def detect_entity_type(document_text: str) -> EntityType:
    """Best-effort entity type detection from extracted/redacted financial text."""
    text = (document_text or "").lower()
    if not text:
        return "unknown"

    llp_signals = [
        r"\bllp\b",
        r"\blimited liability partnership\b",
        r"\bmembers['’]\s+interests\b",
        r"\bloans and other debt due to members\b",
    ]
    company_signals = [
        r"\bcompany number\b",
        r"\bprivate company limited by shares\b",
        r"\bprivate company limited by guarantee\b",
        r"\bpublic limited company\b",
        r"\bltd\b",
        r"\blimited\b",
    ]

    has_llp = any(re.search(p, text, re.IGNORECASE) for p in llp_signals)
    has_company = any(re.search(p, text, re.IGNORECASE) for p in company_signals)

    if has_llp and not has_company:
        return "llp"
    if has_company and not has_llp:
        return "company"
    if has_llp:
        # Prefer LLP when both appear (e.g. references in comparative text).
        return "llp"
    if has_company:
        return "company"
    return "unknown"


def _entity_rule(item: dict) -> Optional[str]:
    vals = {
        str(r.get("value_json", "")).lower()
        for r in (item.get("applicability_rules") or [])
        if isinstance(r, dict) and r.get("rule_type") == "entity_type"
    }
    if vals == {"company"}:
        return "company"
    if vals == {"llp"}:
        return "llp"
    return None


def apply_entity_gate_to_assessments(
    items: list[dict], assessments: list[Assessment], entity_type: EntityType
) -> None:
    """Mutates assessments in place with deterministic entity gating."""
    if entity_type == "unknown":
        return
    item_by_id = {it.get("id"): it for it in items}
    for a in assessments:
        item = item_by_id.get(a.item_id) or {}
        rule = _entity_rule(item)
        if not rule:
            continue
        if rule != entity_type:
            a.status = "not_applicable"
            a.evidence = ""
            a.evidence_location = None
            a.evidence_snippet = None
            a.reasoning = (
                f"Deterministic override: item applies to {rule} entities only; "
                f"document entity detected as {entity_type}."
            )
            a.confidence = max(a.confidence, 0.95)


def apply_entity_gate_to_reviewed(
    items: list[dict], reviewed: list[ReviewedResult], entity_type: EntityType
) -> None:
    """Mutates reviewed results in place with deterministic entity gating."""
    if entity_type == "unknown":
        return
    item_by_id = {it.get("id"): it for it in items}
    for r in reviewed:
        item = item_by_id.get(r.item_id) or {}
        rule = _entity_rule(item)
        if not rule:
            continue
        if rule != entity_type:
            r.changed = True
            r.final_status = "not_applicable"
            r.evidence = ""
            r.evidence_location = None
            r.evidence_snippet = None
            r.reasoning = (
                f"Deterministic override: item applies to {rule} entities only; "
                f"document entity detected as {entity_type}."
            )
            r.confidence = max(r.confidence, 0.95)
