"""
Build full requirement text for checklist items.

XLS → JSON stores one Excel row per item: sub-bullets often contain only a short
fragment (e.g. "its main terms;") while parent rows hold the statutory context
and may be marked is_header (not assessed). The LLM and reviewers still need the
full rule, so we concatenate ancestor requirements + the leaf.
"""

from __future__ import annotations


def index_items_by_id(checklist: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for section in checklist.get("sections", []):
        for item in section.get("items", []):
            iid = item.get("id")
            if isinstance(iid, str):
                out[iid] = item
    return out


def ancestor_ids(item_id: str, all_ids: set[str]) -> list[str]:
    """Return direct ancestor ids top-down (e.g. 5.01, 5.01.c for leaf 5.01.c.i)."""
    ancestors = [oid for oid in all_ids if item_id.startswith(oid + ".")]
    ancestors.sort(key=len)
    return ancestors


def compose_requirement_context(checklist: dict, item_id: str) -> str:
    """
    Join requirement text from every ancestor row in the checklist plus the leaf.
    Uses only ids that exist in the JSON (derived from the same XLS source).
    """
    id_to = index_items_by_id(checklist)
    all_ids = set(id_to.keys())
    parts: list[str] = []
    for aid in ancestor_ids(item_id, all_ids):
        req = (id_to.get(aid) or {}).get("requirement") or ""
        t = str(req).strip()
        if t:
            parts.append(t)
    leaf = id_to.get(item_id) or {}
    leaf_req = str(leaf.get("requirement") or "").strip()
    if leaf_req:
        parts.append(leaf_req)
    return "\n\n".join(parts)


def enrich_items_for_llm(checklist: dict, items: list[dict]) -> list[dict]:
    """Copies items with requirement replaced by composed context (prompt-only)."""
    return [
        {
            **item,
            "requirement": compose_requirement_context(checklist, item["id"]),
        }
        for item in items
    ]
