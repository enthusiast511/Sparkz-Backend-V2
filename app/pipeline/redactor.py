"""
PII Redactor
============
Strips a small amount of contact PII before the LLM sees the document.

We intentionally do NOT redact:
  - Company / charity registration numbers (needed for checklist evidence)
  - Amounts, dates in accounts, postcodes, or other numeric / address disclosure text
  - Organisation or place names (spaCy ORG/GPE) — required for statutory wording

Modes (see settings.REDACTION_MODE):
  none    — return text unchanged
  minimal — email + UK phone only (default)
  names   — minimal + spaCy PERSON (optional policy)
"""

import re

from app.config import settings
from app.pipeline.extractor import ExtractedDocument

PATTERNS: dict[str, re.Pattern] = {
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "PHONE": re.compile(r"\b(?:07\d{9}|\+447\d{9}|\+44\s7\d{9}|0\d{10})\b"),
}

_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def redact_document(doc: ExtractedDocument) -> tuple[str, dict[str, str]]:
    """
    Apply light redaction according to REDACTION_MODE.
    Financial figures and company registration numbers are never masked.
    """
    text = doc.full_text
    mode = settings.REDACTION_MODE

    if mode == "none":
        return text, {}

    redaction_map: dict[str, str] = {}
    counter: dict[str, int] = {}

    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            original = match.group()
            if original not in redaction_map:
                count = counter.get(label, 0) + 1
                counter[label] = count
                redaction_map[original] = f"[{label}_{count}]"

    if mode == "names":
        nlp = _get_nlp()
        spacy_doc = nlp(text)
        for ent in spacy_doc.ents:
            if ent.label_ != "PERSON":
                continue
            if len(ent.text.strip()) < 3:
                continue
            if ent.text not in redaction_map:
                label = ent.label_
                count = counter.get(label, 0) + 1
                counter[label] = count
                redaction_map[ent.text] = f"[{label}_{count}]"

    redacted = text
    for original, placeholder in sorted(redaction_map.items(), key=lambda x: len(x[0]), reverse=True):
        redacted = redacted.replace(original, placeholder)

    return redacted, redaction_map
