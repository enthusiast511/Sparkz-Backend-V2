"""
Reviewer
========
A second LLM pass that reviews the assessor's work and catches common errors,
particularly "missing" items that should be "not_applicable".

Input:  redacted_text, checklist_items, initial assessments
Output: list of reviewed result dicts
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.config import settings
from app.pipeline.assessor import Assessment
from app.prompts.review import REVIEW_SYSTEM_PROMPT, build_review_prompt
from app.utils.openai_retry import chat_completions_create_with_retry


def _optional_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


_EVIDENCE_PLACEHOLDERS = frozenset(
    {
        "keep",
        "same",
        "unchanged",
        "ok",
        "yes",
        "n/a",
        "na",
        "none",
        "as above",
        "see above",
    }
)


def _normalize_review_evidence(raw: str, initial_evidence: str, final_status: str) -> str:
    """Reviewer models often echo 'keep' from prompt shorthand; replace with assessor text."""
    s = (raw or "").strip()
    if final_status in ("missing", "not_applicable"):
        return s
    low = s.lower()
    if not s or low in _EVIDENCE_PLACEHOLDERS:
        return (initial_evidence or "").strip()
    return s


@dataclass
class ReviewedResult:
    item_id: str
    original_status: str
    final_status: str
    changed: bool
    evidence: str
    reasoning: str
    confidence: float
    evidence_location: Optional[str] = None
    evidence_snippet: Optional[str] = None


async def review_batch(
    client,
    redacted_text: str,
    items_with_assessments: list[dict],
    model: str,
) -> list[ReviewedResult]:
    """Review one batch of assessments."""
    prompt = build_review_prompt(redacted_text, items_with_assessments)

    response = await chat_completions_create_with_retry(
        client,
        model=model,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=settings.TEMPERATURE,
        response_format={"type": "json_object"},
    )

    raw = json.loads(response.choices[0].message.content)

    if isinstance(raw, list):
        results_list = raw
    else:
        results_list = next(
            (v for v in raw.values() if isinstance(v, list)),
            []
        )

    reviewed = []
    for r in results_list:
        final_status = r.get("final_status", r.get("status", "missing"))
        loc = _optional_str(r.get("evidence_location"))
        snip = _optional_str(r.get("evidence_snippet"))
        if final_status in ("missing", "not_applicable"):
            loc, snip = None, None
        reviewed.append(ReviewedResult(
            item_id=r.get("item_id", ""),
            original_status=r.get("original_status", ""),
            final_status=final_status,
            changed=bool(r.get("changed", False)),
            evidence=r.get("evidence", "") or "",
            reasoning=r.get("reasoning", "") or "",
            confidence=float(r.get("confidence", 0.5)),
            evidence_location=loc,
            evidence_snippet=snip,
        ))

    return reviewed


async def run_review(
    client,
    redacted_text: str,
    checklist_items: list[dict],
    assessments: list[Assessment],
    entity_type: Optional[str] = None,
    model: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[ReviewedResult]:
    """Review all assessments in sequential batches."""
    model = model or settings.REVIEW_MODEL
    batch_size = settings.BATCH_SIZE

    # Zip items + assessments into combined dicts
    assessment_by_id = {a.item_id: a for a in assessments}
    items_with_assessments = []
    for item in checklist_items:
        item_id = item["id"]
        assessment = assessment_by_id.get(item_id)
        items_with_assessments.append({
            "item_id": item_id,
            "requirement": item.get("requirement", ""),
            "guidance": item.get("guidance", ""),
            "applicability_hint": item.get("applicability_hint", ""),
            "applicability_rules": item.get("applicability_rules", []),
            "entity_type": entity_type or "unknown",
            "initial_assessment": {
                "status": assessment.status if assessment else "missing",
                "evidence": assessment.evidence if assessment else "",
                "evidence_location": assessment.evidence_location if assessment else None,
                "evidence_snippet": assessment.evidence_snippet if assessment else None,
                "reasoning": assessment.reasoning if assessment else "",
                "confidence": assessment.confidence if assessment else 0.5,
            },
        })

    all_reviewed: list[ReviewedResult] = []
    batches = [
        items_with_assessments[i:i + batch_size]
        for i in range(0, len(items_with_assessments), batch_size)
    ]

    for batch in batches:
        batch_results = await review_batch(client, redacted_text, batch, model)
        by_id = {r.item_id: r for r in batch_results}
        for row in batch:
            res = by_id.get(row["item_id"])
            if res is None:
                continue
            init = row["initial_assessment"]
            res.evidence = _normalize_review_evidence(
                res.evidence, init.get("evidence") or "", res.final_status
            )
            if res.final_status not in ("missing", "not_applicable"):
                if res.evidence_location is None:
                    res.evidence_location = _optional_str(init.get("evidence_location"))
                if res.evidence_snippet is None:
                    res.evidence_snippet = _optional_str(init.get("evidence_snippet"))
        all_reviewed.extend(batch_results)
        if on_progress:
            on_progress(len(all_reviewed), len(items_with_assessments))

    return all_reviewed
