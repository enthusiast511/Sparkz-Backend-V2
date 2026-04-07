"""
Checklist Enrichment Script
============================
Uses GPT-4o to generate 'guidance' and 'applicability_hint' fields for all
items in frs105.json and frs102.json.  Existing non-empty fields are skipped
by default (making the script safe to re-run incrementally).

Usage:
    cd backend/
    python scripts/enrich_checklists.py                    # both standards
    python scripts/enrich_checklists.py --standard frs105  # one standard
    python scripts/enrich_checklists.py --dry-run          # print, no write
    python scripts/enrich_checklists.py --force            # re-enrich all

Requires:
    OPENAI_API_KEY in .env (or environment)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).parent.parent
CHECKLIST_DIR = BACKEND_DIR / "app" / "checklists"
ENV_FILE = BACKEND_DIR / ".env"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.checklists.normalize import (  # noqa: E402
    entity_applicability_hint_for_item,
    normalize_checklist,
)

STANDARDS = {
    "frs105": {
        "file": CHECKLIST_DIR / "frs105.json",
        "label": "FRS 105 (Micro-entity accounts)",
    },
    "frs102": {
        "file": CHECKLIST_DIR / "frs102.json",
        "label": "FRS 102 Section 1A (Small company accounts)",
    },
}

ENRICH_MODEL = "gpt-4o"
BATCH_SIZE = 20

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are an expert in UK accounting standards FRS 105 and FRS 102 Section 1A.
Your task is to enrich checklist items used by an automated AI auditor that reads
redacted PDF financial statements and checks disclosure compliance.

For each checklist item you receive, provide two fields:

1. "guidance": One concise sentence (max 25 words) describing what text, note, or
   figure to look for in a UK financial statement PDF to assess this disclosure.
   Focus on WHERE evidence would appear (e.g. "in the notes", "on the face of the
   balance sheet", "in the directors' report"), not on restating the requirement.

2. "applicability_hint": The specific condition under which this item would be NOT
   APPLICABLE (N/A) for a particular company. Write it as a plain-English condition
   an assessor can check against the document content (e.g. "N/A if the company has
   no employees.", "N/A if there are no related party transactions during the period.").
   Use an empty string "" if this disclosure is ALWAYS required with no exceptions.

Return a JSON array ONLY — no prose, no markdown code fences.
Format exactly: [{"id": "...", "guidance": "...", "applicability_hint": "..."}, ...]
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env without requiring python-dotenv (manual parse is fine)."""
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _get_client():
    """Return a synchronous OpenAI client."""
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("ERROR: openai package not installed. Run: pip install openai")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        sys.exit("ERROR: OPENAI_API_KEY not set. Add it to backend/.env")
    return OpenAI(api_key=api_key)


def _flatten_items(checklist: dict) -> List[dict]:
    """Return a flat list of all items across all sections."""
    items = []
    for section in checklist.get("sections", []):
        items.extend(section.get("items", []))
    return items


def _build_index(checklist: dict) -> dict[str, tuple[int, int]]:
    """Map item_id → (section_index, item_index) for in-place updates."""
    index: dict[str, tuple[int, int]] = {}
    for si, section in enumerate(checklist.get("sections", [])):
        for ii, item in enumerate(section.get("items", [])):
            index[item["id"]] = (si, ii)
    return index


def _needs_enrichment(item: dict, force: bool) -> bool:
    if force:
        return True
    return not item.get("guidance", "")


def _enrich_batch(
    client,
    standard_label: str,
    batch: List[dict],
    dry_run: bool,
) -> Optional[List[dict]]:
    """
    Call GPT-4o for a batch of items.
    Returns list of {id, guidance, applicability_hint} or None on failure.
    """
    payload = json.dumps(
        [{"id": item["id"], "requirement": item["requirement"]} for item in batch],
        indent=2,
    )
    user_content = f"Standard: {standard_label}\n\nItems to enrich:\n{payload}"

    if dry_run:
        print(f"  [DRY-RUN] Would call GPT-4o with {len(batch)} items")
        # Return plausible placeholder output for dry-run validation
        return [
            {
                "id": item["id"],
                "guidance": "(dry-run placeholder)",
                "applicability_hint": "",
            }
            for item in batch
        ]

    try:
        response = client.chat.completions.create(
            model=ENRICH_MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        raw = response.choices[0].message.content.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        enriched = json.loads(raw)
        if not isinstance(enriched, list):
            print(f"  [WARN] Unexpected response shape — skipping batch")
            return None
        return enriched

    except Exception as exc:
        print(f"  [WARN] API error: {exc} — skipping batch")
        return None


# ---------------------------------------------------------------------------
# Core enrichment logic
# ---------------------------------------------------------------------------

def enrich_standard(
    key: str,
    client,
    force: bool,
    dry_run: bool,
) -> None:
    meta = STANDARDS[key]
    path: Path = meta["file"]
    label: str = meta["label"]

    if not path.exists():
        print(f"[SKIP] {path} not found")
        return

    with open(path) as f:
        checklist = json.load(f)

    normalize_checklist(checklist)
    all_items = _flatten_items(checklist)
    to_enrich = [
        item
        for item in all_items
        if _needs_enrichment(item, force)
        and item.get("category") != "note"
        and not item.get("is_header")
    ]

    if not to_enrich:
        print(f"[{key.upper()}] All {len(all_items)} items already enriched. Use --force to redo.")
        return

    print(f"\n[{key.upper()}] {len(to_enrich)}/{len(all_items)} items to enrich")

    index = _build_index(checklist)
    enriched_count = 0
    batches = [to_enrich[i : i + BATCH_SIZE] for i in range(0, len(to_enrich), BATCH_SIZE)]
    total_batches = len(batches)

    for batch_num, batch in enumerate(batches, 1):
        first_id = batch[0]["id"]
        last_id = batch[-1]["id"]
        print(f"  batch {batch_num}/{total_batches} ({first_id}–{last_id}) ...", end=" ", flush=True)

        results = _enrich_batch(client, label, batch, dry_run)

        if results is None:
            print("SKIPPED")
            continue

        # Merge results back into checklist
        result_map = {r["id"]: r for r in results if isinstance(r, dict)}
        for item in batch:
            enriched = result_map.get(item["id"])
            if not enriched:
                continue
            si, ii = index[item["id"]]
            target = checklist["sections"][si]["items"][ii]
            target["guidance"] = enriched.get("guidance", "")
            fixed_hint = entity_applicability_hint_for_item(target)
            if fixed_hint is not None:
                target["applicability_hint"] = fixed_hint
            else:
                target["applicability_hint"] = enriched.get("applicability_hint", "")
            enriched_count += 1

        print("OK")

    if not dry_run:
        with open(path, "w") as f:
            json.dump(checklist, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[{key.upper()}] Wrote {path} — enriched {enriched_count}/{len(to_enrich)} items")
    else:
        print(f"[{key.upper()}] Dry run complete — no files written")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich checklist JSON files with GPT-4o guidance and applicability hints."
    )
    parser.add_argument(
        "--standard",
        choices=list(STANDARDS.keys()),
        default=None,
        help="Only enrich a specific standard (default: both)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without calling the API or writing files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enrich items that already have guidance",
    )
    args = parser.parse_args()

    _load_env()
    client = _get_client()

    standards_to_run = [args.standard] if args.standard else list(STANDARDS.keys())

    for key in standards_to_run:
        enrich_standard(key, client, force=args.force, dry_run=args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
