"""
Normalise checklist JSON in memory or on disk.

- Marks parent rows as is_header when a child id exists (id + '.' prefix).
- Sets category \"note\" for guidance-only rows, not assessed.
- Derives applicability_hint from applicability_rules or legacy short hints.

Optional overrides: ids that have children but must still be assessed (e.g. split bullets).
"""

from __future__ import annotations

import re

# Items that have sub-ids in the XLS hierarchy but are still disclosure bullets to assess.
_FORCE_ASSESSABLE: frozenset[str] = frozenset({"1.01.h"})

# Known XLS conversion quirks that should be corrected at load time.
_ID_REMAP: dict[str, str] = {
    "1.01.h.i": "1.01.i",
}


def is_guidance_note_id(item_id: str) -> bool:
    """
    True if this checklist item id denotes a guidance / note row, not an assessable requirement.

    Uses dot-separated path segments so ids like ``1.annotation`` or ``2.notation`` are not
    treated as notes (substring ``note`` is not present). Segments equal to ``note``,
    ``note_*``, ``note`` + digits, or containing ``note`` (e.g. ``guidance_note``) match.
    """
    if not item_id or not isinstance(item_id, str):
        return False
    for seg in item_id.split("."):
        sl = seg.lower()
        if sl == "note" or sl.startswith("note_") or re.fullmatch(r"note\d+", sl, re.IGNORECASE):
            return True
        if "note" in sl:
            return True
    return False


def _is_structured_line_requirement(text: str) -> bool:
    """
    Heuristic: true for short/tabular line-items that should remain assessable
    (e.g. "Fixed assets;", "Capital and reserves.").
    """
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if t.endswith(";") or t.endswith(":"):
        return True
    words = t.split()
    if len(words) <= 8 and not any(
        tok in low
        for tok in (
            " may ",
            " shall ",
            " should ",
            " must ",
            " can ",
            " could ",
            " would ",
            " allows ",
            " allow ",
        )
    ):
        return True
    return False


def _is_structural_child(parent_id: str, candidate_id: str) -> bool:
    """
    True when candidate_id represents a real requirement child of parent_id.

    Excludes continuation rows (e.g. ".L1") and note/guidance rows that should not
    turn a requirement into a non-assessable header.
    """
    if candidate_id == parent_id or not candidate_id.startswith(parent_id + "."):
        return False
    remainder = candidate_id[len(parent_id) + 1 :]
    direct_seg = remainder.split(".", 1)[0]
    if re.fullmatch(r"L\d+", direct_seg, re.IGNORECASE):
        return False
    if is_guidance_note_id(direct_seg):
        return False
    return True


def entity_applicability_hint_for_item(item: dict) -> str | None:
    """If item has entity_type applicability rules, return the canonical hint string."""
    return _hint_from_entity_rules(item.get("applicability_rules"))


def _hint_from_entity_rules(rules: list | None) -> str | None:
    if not rules:
        return None
    vals: set[str] = set()
    for r in rules:
        if not isinstance(r, dict):
            continue
        if r.get("rule_type") != "entity_type":
            continue
        v = r.get("value_json")
        if isinstance(v, str):
            vals.add(v.lower())
    if vals == {"company"}:
        return "N/A for LLPs — this disclosure is required for companies only."
    if vals == {"llp"}:
        return "N/A for companies — this disclosure is required for LLPs only."
    if len(vals) > 1 and vals <= {"company", "llp"}:
        return (
            "Source column E marks both companies (C) and LLPs (L) for this row; "
            "resolve applicability before treating an item as Missing vs N/A."
        )
    return None


def _hint_from_legacy_text(hint: str) -> str:
    h = (hint or "").strip()
    if not h:
        return ""
    low = h.lower()
    if "companies only" in low and "llp" not in low:
        return "N/A for LLPs — this disclosure is required for companies only."
    if "llps only" in low:
        return "N/A for companies — this disclosure is required for LLPs only."
    if "llp" in low and "only" in low and "compan" not in low:
        return "N/A for companies — this disclosure is required for LLPs only."
    return h


def normalize_checklist(checklist: dict) -> dict:
    """
    Mutate checklist sections/items in place and return checklist.
    """
    all_ids: list[str] = []
    for section in checklist.get("sections", []):
        for item in section.get("items", []):
            iid = item.get("id")
            if isinstance(iid, str):
                remapped = _ID_REMAP.get(iid)
                if remapped:
                    item["id"] = remapped
                    iid = remapped
                all_ids.append(iid)
    id_set = set(all_ids)

    for section in checklist.get("sections", []):
        for item in section.get("items", []):
            iid = item.get("id")
            if not isinstance(iid, str):
                continue

            # Backward-compat correction: old generated JSON may have marked all .L* rows
            # as note, but many are real structured disclosure line-items.
            if re.search(r"(?:^|\.)L\d+$", iid, re.IGNORECASE):
                req = str(item.get("requirement") or "")
                if _is_structured_line_requirement(req):
                    item["category"] = "general"

            if is_guidance_note_id(iid):
                item["category"] = "note"

            has_child = any(_is_structural_child(iid, oid) for oid in id_set)
            if item.get("is_header") is False:
                pass
            elif iid in _FORCE_ASSESSABLE:
                item["is_header"] = False
            elif has_child:
                item["is_header"] = True
            else:
                item["is_header"] = False

            from_rules = _hint_from_entity_rules(item.get("applicability_rules"))
            if from_rules is not None:
                item["applicability_hint"] = from_rules
            else:
                item["applicability_hint"] = _hint_from_legacy_text(
                    item.get("applicability_hint", "")
                )

    return checklist
