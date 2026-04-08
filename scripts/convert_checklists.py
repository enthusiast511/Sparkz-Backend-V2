"""
XLS → JSON Checklist Converter
================================
Reuses the Phase 1 parsing logic from sparkz_backend/app/services/excel_importer.py
(read_workbook, normalize_rows, build_hierarchy, etc.) and converts the resulting
NodeRecord objects into the JSON format required by the new pipeline.

Usage:
    cd backend/
    python scripts/convert_checklists.py

Writes:
    app/checklists/frs105.json
    app/checklists/frs102.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models (copied from excel_importer.py — no Supabase dependency)
# ─────────────────────────────────────────────────────────────────────────────


class ReferenceRecord(BaseModel):
    reference_type: str
    citation: str
    label: Optional[str] = None
    sort_order: int = 0


class ApplicabilityRecord(BaseModel):
    rule_type: str
    operator: str = "="
    value_json: Any
    description: Optional[str] = None
    sort_order: int = 0


class NodeRecord(BaseModel):
    code: Optional[str]
    display_number: Optional[str]
    title: str
    full_text: Optional[str]
    node_type: str
    level: int
    sort_order: int
    path: str
    parent_code: Optional[str]
    answer_type: str = "yn_na"
    is_answerable: bool = False
    is_required: bool = False
    metadata: dict = {}
    references: list[ReferenceRecord] = []
    applicability_rules: list[ApplicabilityRecord] = []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 parsing (copied verbatim from excel_importer.py)
# ─────────────────────────────────────────────────────────────────────────────

_SECTION_RE     = re.compile(r"^\d+$")
_QUESTION_RE    = re.compile(r"^\d+\.\d+$")
_SUBQUES_RE     = re.compile(r"^\([a-z]\)$")
_SUBQ_INLINE_RE = re.compile(r"^\(([a-z]+)\)\s+(.+)", re.DOTALL)
_ROMAN_SMALL_RE = re.compile(r"^(x{0,3})(ix|iv|v?i{0,3})$", re.IGNORECASE)


# Numbered notes in the XLS that explain the checklist (not disclosure requirements).
_NOTE_NUMBERED_EXPLAINER = re.compile(r"^\s*Note\s+\d+\.\s", re.I)
# ICAEW / ACCA boilerplate blocks (not checkable rules).
_WORDING_BELOW_TECH = re.compile(r"^\s*The wording below is based on", re.I)
_S_DOES_NOT_REQUIRE = re.compile(r"^\s*s\d+\s+does not require", re.I)
_ACCOUNTANTS_COMPILATION_LINE = re.compile(
    r"^\s*Accountant'?s reports on the compilation of financial statements\s*$",
    re.I,
)


def _skip_excel_narrative_guidance(col_a: str, col_b: str) -> bool:
    """
    True → omit this row from JSON entirely (explanatory / policy text from the workbook,
    not statutory disclosure requirements).
    """
    b = (col_b or "").strip()
    if not b:
        return False
    low = b.lower()
    if _NOTE_NUMBERED_EXPLAINER.match(b):
        return True
    if _WORDING_BELOW_TECH.match(b):
        return True
    if _S_DOES_NOT_REQUIRE.match(b):
        return True
    if _ACCOUNTANTS_COMPILATION_LINE.match(b):
        return True
    # Duplicate rubric line (section title repeated in col B with no code)
    if re.match(r"^\s*accountant'?s reports\s*$", b, re.I):
        return True
    if "tech 07/16" in low and ("icaew" in low or "acca" in low) and len(b) > 70:
        return True
    if "factsheet 163" in low and "acca" in low and len(b) > 50:
        return True
    return False


def _is_guidance_note_row(col_b: str) -> bool:
    """True for prose guidance rows that should stay out of the assessable checklist."""
    b = (col_b or "").strip()
    if not b:
        return False
    low = b.lower()
    if "[guidance]" in low or low.startswith("guidance"):
        return True
    if low.startswith("per the above"):
        return True
    if "table of equivalence" in low and "terminology" in low:
        return True
    return False


def _looks_like_structured_line_item(col_b: str) -> bool:
    """
    Heuristic for blank-col-A rows that should remain as parent continuation lines
    (e.g. Statement of financial position line items) rather than section guidance notes.
    """
    b = (col_b or "").strip()
    if not b:
        return False
    low = b.lower()
    if _is_guidance_note_row(b):
        return False
    if low.startswith("profit or loss for the financial year"):
        return True
    # Typical tabular/list line items in the statement formats.
    if b.endswith(";") or b.endswith(":"):
        return True
    words = b.split()
    # Short noun-phrase style rows (often final item in a list, may end with ".")
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


def _int_to_roman(n: int) -> str:
    vals = [10, 9, 5, 4, 1]
    syms = ["x", "ix", "v", "iv", "i"]
    result = ""
    for val, sym in zip(vals, syms):
        while n >= val:
            result += sym
            n -= val
    return result


def read_workbook(file_path: str | Path) -> dict[str, pd.DataFrame]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Workbook not found: {path}")
    xl = pd.ExcelFile(path, engine="openpyxl")
    return {name: xl.parse(name, header=None, dtype=str) for name in xl.sheet_names}


def classify_node_type(col_a: str, col_b: str = "") -> Optional[str]:
    a = str(col_a).strip() if col_a else ""
    if a:
        if _SECTION_RE.match(a):
            return "section"
        if _QUESTION_RE.match(a):
            return "question"
        if _SUBQUES_RE.match(a):
            return "subquestion"
        return None
    b = str(col_b).strip() if col_b else ""
    if b and _SUBQ_INLINE_RE.match(b):
        return "subquestion"
    return None


def _build_path(
    node_type: str,
    code: str,
    current_section: Optional[str],
    current_question: Optional[str],
    current_subquestion: Optional[str] = None,
) -> str:
    if node_type == "section":
        return code
    if node_type == "question":
        return f"{current_section}/{code}"
    if node_type == "subquestion":
        if current_subquestion:
            return f"{current_section}/{current_question}/{current_subquestion}/{code}"
        return f"{current_section}/{current_question}/{code}"
    return code


def parse_references(raw: str, sort_start: int = 0) -> list[ReferenceRecord]:
    if not raw or not str(raw).strip():
        return []
    parts = re.split(r"[;\n|]+", str(raw).strip())
    records: list[ReferenceRecord] = []
    for i, part in enumerate(parts):
        citation = part.strip()
        if not citation:
            continue
        upper = citation.upper()
        if any(kw in upper for kw in ("FRS", "IFRS", "IAS", "SSAP", "GAAP", "UITF")):
            ref_type = "standard"
        elif any(kw in citation for kw in ("Act", " s.", "Reg", "SI ", "LLP Regulations")):
            ref_type = "law"
        elif any(kw in upper for kw in ("GUIDANCE", "PRACTICE NOTE", "PN ", "TECH ")):
            ref_type = "guidance"
        else:
            ref_type = "other"
        records.append(ReferenceRecord(reference_type=ref_type, citation=citation, sort_order=sort_start + i))
    return records


def parse_applicability(raw: str) -> list[ApplicabilityRecord]:
    """
    Parse XLS column E into entity_type rules. Supports isolated C/L flags and common phrases
    (e.g. \"Companies only\", \"LLP\"). Duplicate signals dedupe to one rule per entity type.
    """
    if not raw or not str(raw).strip():
        return []
    raw_upper = str(raw).strip().upper()
    seen: set[str] = set()
    rules: list[ApplicabilityRecord] = []

    def add_rule(value: str, desc: str) -> None:
        if value in seen:
            return
        seen.add(value)
        rules.append(
            ApplicabilityRecord(
                rule_type="entity_type",
                operator="=",
                value_json=value,
                description=desc,
                sort_order=len(rules),
            )
        )

    # Phrase-level hints (column E is not always a single letter).
    if re.search(r"\bCOMPAN(?:Y|IES)(?:\s+ONLY)?\b", raw_upper) or re.search(
        r"\bCO\.?\s+ONLY\b", raw_upper
    ):
        add_rule("company", "Applicable to companies")
    if re.search(r"\bLLPs?\b(?:\s+ONLY)?\b", raw_upper):
        add_rule("llp", "Applicable to LLPs")

    flag_map = {
        "C": ("company", "Applicable to companies"),
        "L": ("llp", "Applicable to LLPs"),
    }
    for flag, (value, desc) in flag_map.items():
        if re.search(rf"(?<![A-Z]){flag}(?![A-Z])", raw_upper):
            add_rule(value, desc)

    return rules


def normalize_rows(sheet_name: str, df: pd.DataFrame, sheet_prefix: str = "") -> list[NodeRecord]:
    nodes: list[NodeRecord] = []
    sort_counter = 0
    _seen_codes: dict[str, int] = {}

    def _make_unique(raw: str) -> str:
        if raw not in _seen_codes:
            _seen_codes[raw] = 1
            return raw
        _seen_codes[raw] += 1
        return f"{raw}_{_seen_codes[raw]}"

    current_section_code:     Optional[str] = None
    current_question_code:    Optional[str] = None
    current_subquestion_code: Optional[str] = None
    current_subsubq_counter:  int           = 0
    # Per-parent counter for structural line items sourced from blank col A rows.
    _line_under_parent: dict[str, int] = {}

    for row_idx, row in df.iterrows():
        raw = [str(v).strip() if pd.notna(v) else "" for v in row]
        raw += [""] * (5 - len(raw))
        col_a, col_b, col_c, col_d, col_e = raw[0], raw[1], raw[2], raw[3], raw[4]

        if _skip_excel_narrative_guidance(col_a, col_b):
            continue

        node_type = classify_node_type(col_a, col_b)
        if node_type is None:
            if not col_a.strip() and col_b.strip() and current_section_code:
                if _is_guidance_note_row(col_b):
                    # Guidance row: must NOT overwrite current_question_code / subquestion,
                    # otherwise the next "(c) ..." subquestion becomes e.g. "2.note.c".
                    narr_base = f"{current_section_code}.note"
                    code = _make_unique(narr_base)
                    path = f"{current_section_code}/{code}" if current_section_code else code
                    nodes.append(
                        NodeRecord(
                            code=code,
                            display_number=None,
                            title=col_b.strip(),
                            full_text=col_d if col_d else None,
                            node_type="guidance_note",
                            level=1,
                            sort_order=sort_counter,
                            path=path,
                            parent_code=current_section_code,
                            answer_type="yn_na",
                            is_answerable=True,
                            is_required=False,
                            metadata={
                                "source_sheet": sheet_name,
                                "source_row": int(row_idx) + 1,
                                "guidance_only": True,
                            },
                            references=parse_references(col_c),
                            applicability_rules=parse_applicability(col_e),
                        )
                    )
                    sort_counter += 1
                    continue
                else:
                    parent_for_line = (
                        current_subquestion_code
                        or current_question_code
                        or current_section_code
                    )
                    if parent_for_line and (
                        _looks_like_structured_line_item(col_b) or bool(col_c.strip())
                    ):
                        _line_under_parent[parent_for_line] = (
                            _line_under_parent.get(parent_for_line, 0) + 1
                        )
                        idx = _line_under_parent[parent_for_line]
                        code = _make_unique(f"{parent_for_line}.L{idx}")
                        title = col_b.strip()
                        path = (
                            f"{current_section_code}/{parent_for_line}/{code}"
                            if current_section_code
                            else code
                        )
                        nodes.append(
                            NodeRecord(
                                code=code,
                                display_number=None,
                                title=title,
                                full_text=col_d if col_d else None,
                                node_type="detail_line",
                                level=2,
                                sort_order=sort_counter,
                                path=path,
                                parent_code=parent_for_line,
                                answer_type="yn_na",
                                is_answerable=True,
                                is_required=False,
                                metadata={
                                    "source_sheet": sheet_name,
                                    "source_row": int(row_idx) + 1,
                                    "from_blank_col_a": True,
                                },
                                references=parse_references(col_c),
                                applicability_rules=parse_applicability(col_e),
                            )
                        )
                    else:
                        # Narrative commentary row => section note, non-assessable.
                        narr_base = f"{current_section_code}.note"
                        code = _make_unique(narr_base)
                        title = col_b.strip()
                        path = f"{current_section_code}/{code}" if current_section_code else code
                        nodes.append(
                            NodeRecord(
                                code=code,
                                display_number=None,
                                title=title,
                                full_text=col_d if col_d else None,
                                node_type="guidance_note",
                                level=1,
                                sort_order=sort_counter,
                                path=path,
                                parent_code=current_section_code,
                                answer_type="yn_na",
                                is_answerable=True,
                                is_required=False,
                                metadata={
                                    "source_sheet": sheet_name,
                                    "source_row": int(row_idx) + 1,
                                    "from_blank_col_a": True,
                                    "continuation_row": True,
                                },
                                references=parse_references(col_c),
                                applicability_rules=parse_applicability(col_e),
                            )
                        )
                    sort_counter += 1
                    continue
            else:
                continue

        if node_type == "subquestion":
            if not col_a.strip():
                m      = _SUBQ_INLINE_RE.match(col_b.strip())
                letter = m.group(1)
                title  = m.group(2).strip()
            else:
                letter = col_a.strip("()")
                title  = col_b if col_b else f"({letter})"

            is_sub_subquestion = (
                current_subquestion_code is not None
                and bool(_ROMAN_SMALL_RE.match(letter))
            )

            if is_sub_subquestion:
                current_subsubq_counter += 1
                roman_suffix   = _int_to_roman(current_subsubq_counter)
                display_number = f"({letter})"
                code           = f"{current_subquestion_code}.{roman_suffix}"
                level          = 3
                parent_code    = current_subquestion_code
            else:
                display_number = f"({letter})"
                if current_question_code:
                    code = _make_unique(f"{current_question_code}.{letter}")
                elif current_section_code:
                    code = _make_unique(f"{current_section_code}.{letter}")
                else:
                    bare = f"{sheet_prefix}({letter})" if sheet_prefix else f"({letter})"
                    code = _make_unique(bare)
                level                    = 2
                parent_code              = current_question_code or current_section_code
                current_subquestion_code = code
                current_subsubq_counter  = 0
        else:
            raw_code = col_a.strip()
            if not raw_code:
                narr_base      = f"{current_section_code}.note"
                code           = _make_unique(narr_base)
                display_number = None
            else:
                code           = _make_unique(f"{sheet_prefix}{raw_code}")
                display_number = raw_code
            title = col_b if col_b else (raw_code or code)
            level = {"section": 0, "question": 1}[node_type]

        if node_type == "section":
            current_section_code     = code
            current_question_code    = None
            current_subquestion_code = None
            current_subsubq_counter  = 0
            parent_code              = None
        elif node_type == "question":
            current_question_code    = code
            current_subquestion_code = None
            current_subsubq_counter  = 0
            parent_code              = current_section_code

        path = _build_path(
            node_type, code, current_section_code, current_question_code,
            current_subquestion_code if (node_type == "subquestion" and level == 3) else None,
        )

        is_answerable = level > 0
        answer_type   = "yn_na" if is_answerable else "none"

        nodes.append(NodeRecord(
            code=code,
            display_number=display_number,
            title=title,
            full_text=col_d if col_d else None,
            node_type=node_type,
            level=level,
            sort_order=sort_counter,
            path=path,
            parent_code=parent_code,
            answer_type=answer_type,
            is_answerable=is_answerable,
            is_required=False,
            metadata={"source_sheet": sheet_name, "source_row": int(row_idx) + 1},
            references=parse_references(col_c),
            applicability_rules=parse_applicability(col_e),
        ))
        sort_counter += 1

    return nodes


def build_hierarchy(all_nodes: list[NodeRecord]) -> list[NodeRecord]:
    codes = {n.code for n in all_nodes if n.code}
    for node in all_nodes:
        if node.parent_code and node.parent_code not in codes:
            print(f"  ⚠  Node '{node.code}' references unknown parent '{node.parent_code}'")
    return all_nodes


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: NodeRecord → new JSON format
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _applicability_hint(rules: list[ApplicabilityRecord]) -> str:
    values = {r.value_json for r in rules if r.rule_type == "entity_type"}
    if not values:
        return ""
    if values == {"company"}:
        return "N/A for LLPs — this disclosure is required for companies only."
    if values == {"llp"}:
        return "N/A for companies — this disclosure is required for LLPs only."
    return ""


def nodes_to_checklist_json(
    nodes: list[NodeRecord],
    standard: str,
    version: str,
) -> dict:
    """Convert flat NodeRecord list into the checklist JSON structure."""
    sections_out = []
    current_section: Optional[dict] = None

    for node in nodes:
        if node.level == 0:
            current_section = {
                "id": f"section_{node.code}",
                "title": node.title,
                "items": [],
            }
            sections_out.append(current_section)
        elif node.is_answerable and current_section is not None:
            rules_out = [
                {
                    "rule_type": r.rule_type,
                    "operator": r.operator,
                    "value_json": r.value_json,
                    "description": r.description,
                    "sort_order": r.sort_order,
                }
                for r in node.applicability_rules
            ]
            current_section["items"].append({
                "id": node.code,
                "requirement": node.title,
                "guidance": node.full_text or "",
                "applicability_rules": rules_out,
                "applicability_hint": _applicability_hint(node.applicability_rules),
                "category": _slugify(current_section["title"]),
                "references": [r.citation for r in node.references],
            })

    # Drop empty sections
    sections_out = [s for s in sections_out if s["items"]]

    return {
        "standard": standard,
        "version": version,
        "sections": sections_out,
    }


def _apply_frs102_structural_cleanup(nodes: list[NodeRecord]) -> int:
    """
    FRS 102-only cleanup for obvious structural headings that should not be assessed.

    Keep this conservative to avoid dropping genuine disclosure requirements.
    """
    changed = 0
    heading_titles = {
        "auditor's reports",
        "assurance review report",
        "compliance with frs 102 1a",
        "frequency of reporting",
        "comparative information",
        "materiality and aggregation",
        "formats",
    }

    for n in nodes:
        code = (n.code or "").strip()
        title = (n.title or "").strip()
        low = title.lower()

        # Section-level blank-code carry lines like "2.L1", "3.L1" are structural labels.
        if re.fullmatch(r"\d+\.L\d+", code):
            if n.is_answerable:
                n.is_answerable = False
                n.node_type = "guidance_note"
                changed += 1
            continue

        # Ultra-short heading captions with no refs/rules are structural in FRS102 workbook.
        if (
            n.node_type == "detail_line"
            and n.is_answerable
            and low in heading_titles
            and not n.references
            and not n.applicability_rules
        ):
            n.is_answerable = False
            n.node_type = "guidance_note"
            changed += 1

    return changed


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

CHECKLISTS = [
    {
        "xls_path": "../FRS105_DC_2025.xlsx",
        "sheet": "Micro DC",
        "standard": "FRS 105",
        "version": "2025.1",
        "out_file": "app/checklists/frs105.json",
    },
    {
        "xls_path": "../FRS1021A_DC_2025.xlsx",
        "sheet": "Small DC",
        "standard": "FRS 102 Section 1A",
        "version": "2025.1",
        "out_file": "app/checklists/frs102.json",
    },
]


def main():
    base = Path(__file__).parent.parent  # backend/

    for cfg in CHECKLISTS:
        xls_path = (base / cfg["xls_path"]).resolve()
        out_path = base / cfg["out_file"]

        print(f"\nConverting {xls_path.name} → {out_path.relative_to(base)}")

        if not xls_path.exists():
            print(
                f"  ✗ File not found: {xls_path}\n"
                f"    Copy the workbook to this path (or adjust xls_path in CHECKLISTS), then re-run."
            )
            continue

        sheets = read_workbook(xls_path)
        print(f"  Sheets: {list(sheets.keys())}")

        if cfg["sheet"] not in sheets:
            print(f"  ⚠  Sheet '{cfg['sheet']}' not found — using first sheet")
            df = next(iter(sheets.values()))
            sheet_name = next(iter(sheets.keys()))
        else:
            df = sheets[cfg["sheet"]]
            sheet_name = cfg["sheet"]

        nodes = normalize_rows(sheet_name, df)
        nodes = build_hierarchy(nodes)
        print(f"  Nodes extracted: {len(nodes)}")

        if str(cfg["standard"]).strip().lower().startswith("frs 102"):
            changed = _apply_frs102_structural_cleanup(nodes)
            if changed:
                print(f"  FRS102 structural rows de-assessed: {changed}")

        checklist = nodes_to_checklist_json(nodes, cfg["standard"], cfg["version"])
        # Normalise headers, note rows, and applicability hints (requires backend on path)
        _backend = Path(__file__).resolve().parent.parent
        if str(_backend) not in sys.path:
            sys.path.insert(0, str(_backend))
        from app.checklists.normalize import normalize_checklist

        normalize_checklist(checklist)
        total_items = sum(len(s["items"]) for s in checklist["sections"])
        print(f"  Sections: {len(checklist['sections'])}, Items: {total_items}")

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(checklist, f, indent=2, ensure_ascii=False)
        print(f"  ✓ Written to {out_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
