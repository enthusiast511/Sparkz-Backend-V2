"""
PDF Extractor
=============
Input:  PDF file path
Output: ExtractedDocument

Uses pdfplumber for text + table extraction. Preserves page numbers
for evidence referencing. Tables are converted to markdown format.
"""

from dataclasses import dataclass, field

import pdfplumber


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    tables: list[str] = field(default_factory=list)


@dataclass
class ExtractedDocument:
    pages: list[ExtractedPage]
    full_text: str
    total_pages: int
    token_estimate: int  # chars / 4


def _table_to_markdown(table: list[list]) -> str:
    """Convert a pdfplumber table (list of rows) to a markdown-style string."""
    if not table:
        return ""
    rows = []
    for row in table:
        cells = [str(cell or "").strip() for cell in row]
        rows.append(" | ".join(cells))
    if len(rows) > 1:
        # Insert separator after header row
        widths = [len(c) for c in rows[0].split(" | ")]
        separator = " | ".join("-" * max(w, 3) for w in widths)
        rows.insert(1, separator)
    return "\n".join(rows)


def extract_pdf(file_path: str) -> ExtractedDocument:
    """Parse a PDF and return structured text with page markers."""
    pages = []

    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            tables = []
            for table in page.extract_tables():
                md = _table_to_markdown(table)
                if md:
                    tables.append(md)
            pages.append(ExtractedPage(page_number=i + 1, text=text, tables=tables))

    full_text = ""
    for page in pages:
        full_text += f"\n--- PAGE {page.page_number} ---\n"
        if page.text:
            full_text += page.text + "\n"
        for table in page.tables:
            full_text += "\n" + table + "\n"

    return ExtractedDocument(
        pages=pages,
        full_text=full_text,
        total_pages=len(pages),
        token_estimate=len(full_text) // 4,
    )
