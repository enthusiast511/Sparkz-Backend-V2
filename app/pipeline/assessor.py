"""
Assessor
========
Sends batches of checklist items to the LLM with the full redacted document.

Input:  redacted_text, checklist_items (list of dicts)
Output: list of Assessment objects
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.config import settings
from app.prompts.assess import SYSTEM_PROMPT, build_assessment_prompt
from app.utils.openai_retry import chat_completions_create_with_retry


def _optional_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


@dataclass
class Assessment:
    item_id: str
    status: str           # met | partially_met | missing | not_applicable
    evidence: str
    reasoning: str
    confidence: float     # 0.0–1.0
    evidence_location: Optional[str] = None
    evidence_snippet: Optional[str] = None


async def assess_batch(
    client,
    redacted_text: str,
    items: list[dict],
    model: str,
) -> list[Assessment]:
    """Send one batch of items to the LLM and return assessments."""
    prompt = build_assessment_prompt(redacted_text, items)

    response = await chat_completions_create_with_retry(
        client,
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=settings.TEMPERATURE,
        response_format={"type": "json_object"},
    )

    raw = json.loads(response.choices[0].message.content)

    # Handle both a direct array and {"assessments": [...]}
    if isinstance(raw, list):
        results_list = raw
    else:
        # Find the first list value
        results_list = next(
            (v for v in raw.values() if isinstance(v, list)),
            []
        )

    assessments = []
    for r in results_list:
        status = r.get("status", "missing")
        loc = _optional_str(r.get("evidence_location"))
        snip = _optional_str(r.get("evidence_snippet"))
        if status in ("missing", "not_applicable"):
            loc, snip = None, None
        assessments.append(Assessment(
            item_id=r.get("item_id", ""),
            status=status,
            evidence=r.get("evidence", "") or "",
            reasoning=r.get("reasoning", "") or "",
            confidence=float(r.get("confidence", 0.5)),
            evidence_location=loc,
            evidence_snippet=snip,
        ))

    return assessments


async def run_assessment(
    client,
    redacted_text: str,
    checklist_items: list[dict],
    model: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[Assessment]:
    """Run assessment across all checklist items in sequential batches."""
    model = model or settings.OPENAI_MODEL
    batch_size = settings.BATCH_SIZE
    all_assessments: list[Assessment] = []

    batches = [
        checklist_items[i:i + batch_size]
        for i in range(0, len(checklist_items), batch_size)
    ]

    for batch in batches:
        batch_results = await assess_batch(client, redacted_text, batch, model)
        all_assessments.extend(batch_results)
        if on_progress:
            on_progress(len(all_assessments), len(checklist_items))

    return all_assessments
