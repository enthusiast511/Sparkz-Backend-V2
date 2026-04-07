"""
Checklist Loader
================
Loads and validates the JSON checklist files for FRS 105 and FRS 102.

Public API:
    load_checklist(standard: str) -> dict
    flatten_checklist_items(checklist: dict) -> list[dict]
"""

import json
from pathlib import Path

from app.checklists.normalize import normalize_checklist

CHECKLISTS_DIR = Path(__file__).parent

_STANDARD_MAP = {
    "frs105": "frs105.json",
    "frs102": "frs102.json",
}


def load_checklist(standard: str) -> dict:
    """
    Load checklist JSON for the given standard.

    Args:
        standard: "frs105" or "frs102" (case-insensitive)

    Returns:
        The full checklist dict with sections and items.

    Raises:
        ValueError if standard is not recognised.
        FileNotFoundError if the JSON file is missing.
    """
    key = standard.lower().strip()
    filename = _STANDARD_MAP.get(key)
    if not filename:
        raise ValueError(f"Unknown standard '{standard}'. Valid values: {list(_STANDARD_MAP)}")

    path = CHECKLISTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Checklist file not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return normalize_checklist(data)


def flatten_checklist_items(checklist: dict) -> list[dict]:
    """
    Flatten assessable items only: excludes guidance notes (category \"note\")
    and header rows (is_header) per checklist spec.
    """
    items = []
    for section in checklist.get("sections", []):
        for item in section.get("items", []):
            if item.get("category") == "note":
                continue
            if item.get("is_header"):
                continue
            items.append(item)
    return items
