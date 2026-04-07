"""
Write normalised checklist JSON to disk (is_header, note category, applicability hints).

Runtime already normalises on load; this script updates the committed files so diffs are visible.

Usage:
    cd backend/
    python scripts/post_process_checklists.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.checklists.normalize import normalize_checklist  # noqa: E402

FILES = [
    BACKEND_DIR / "app" / "checklists" / "frs105.json",
    BACKEND_DIR / "app" / "checklists" / "frs102.json",
]


def main() -> None:
    for path in FILES:
        if not path.exists():
            print(f"[SKIP] {path}")
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        normalize_checklist(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        n = sum(len(s.get("items", [])) for s in data.get("sections", []))
        print(f"[OK] {path.name} — {n} items normalised")


if __name__ == "__main__":
    main()
