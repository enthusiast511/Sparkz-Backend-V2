# Sparkz DCT — Backend v2

FastAPI backend for the **Disclosure Checklist Tool (DCT)** — v2 LLM-first architecture.

Replaces the old vector-embedding pipeline (`sparkz_backend/`) with a full-document approach:
PDF → PII redaction → batched GPT-4o assessment → GPT-4o-mini reviewer pass → structured results.

- **Local:** `http://localhost:8002`
- **Swagger UI:** `http://localhost:8002/docs`

---

## Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app + all API endpoints
│   ├── config.py            # Settings loaded from .env
│   ├── models.py            # SQLAlchemy models (SQLite)
│   ├── schemas.py           # Pydantic response schemas
│   ├── pipeline/
│   │   ├── extractor.py     # PDF text extraction (pdfplumber)
│   │   ├── redactor.py      # PII redaction (regex + spaCy NER)
│   │   ├── assessor.py      # Batched GPT-4o assessment
│   │   ├── reviewer.py      # GPT-4o-mini reviewer pass
│   │   └── orchestrator.py  # Pipeline wiring + SSE progress state
│   ├── checklists/
│   │   ├── loader.py        # Load + flatten checklist JSON
│   │   ├── frs105.json      # FRS 105 (Micro-entity) checklist
│   │   └── frs102.json      # FRS 102 Section 1A (Small company) checklist
│   ├── prompts/
│   │   ├── assess.py        # System + user prompt for assessor
│   │   └── review.py        # System + user prompt for reviewer
│   └── utils/
│       └── openai_client.py # Singleton AsyncOpenAI client
├── data/                    # Gitignored — uploaded PDFs + sparkz.db (SQLite)
├── scripts/
│   ├── convert_checklists.py  # XLS → JSON converter (run once)
│   └── enrich_checklists.py   # GPT-4o guidance enrichment
├── requirements.txt
├── run.py                   # Entry point
└── .env                     # Not committed — copy from .env.example
```

---

## Local Setup

**Prerequisites:** Python 3.9+

```bash
cd backend/

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Download the spaCy language model (required for PII redaction)
python -m spacy download en_core_web_sm

# Configure environment
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=sk-...

# Start the server
python run.py
```

The API will be available at `http://localhost:8002`. Hot reload is enabled — the server
restarts automatically when you change a `.py` file.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the required value:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | **Yes** | — | Your OpenAI API key |
| `OPENAI_MODEL` | No | `gpt-4o` | Model used for the assessment pass |
| `REVIEW_MODEL` | No | `gpt-4o-mini` | Model used for the reviewer pass |
| `BATCH_SIZE` | No | `12` | Checklist items per LLM call |
| `TEMPERATURE` | No | `0.1` | LLM temperature (lower = more deterministic) |
| `MAX_TOKENS_FULL_DOC` | No | `80000` | Documents larger than this are truncated |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/analyse` | Upload PDF + standard, start analysis |
| `GET` | `/api/analyse/{run_id}/progress` | SSE stream — real-time pipeline progress |
| `GET` | `/api/results/{run_id}` | Full results for a completed run |
| `PATCH` | `/api/results/{run_id}/items/{item_id}` | Human override (status + notes) |
| `GET` | `/api/results/{run_id}/export` | Download results as CSV |
| `GET` | `/api/runs` | List all past runs (most recent first) |

### Start an analysis

```bash
curl -X POST http://localhost:8002/api/analyse \
  -F "file=@accounts.pdf" \
  -F "standard=frs105"
```

Response:
```json
{ "run_id": "550e8400-...", "status": "pending" }
```

### Stream progress

```bash
curl -N http://localhost:8002/api/analyse/550e8400-.../progress
```

SSE events:
```
data: {"stage": "extract", "detail": "Parsing PDF...", "pct": 5}
data: {"stage": "redact",  "detail": "Removing PII...", "pct": 15}
data: {"stage": "assess",  "detail": "Assessing batch 3/11...", "pct": 45}
data: {"stage": "review",  "detail": "Reviewing batch 2/11...", "pct": 82}
data: {"stage": "complete","detail": "Done", "pct": 100}
```

### Get results

```bash
curl http://localhost:8002/api/results/550e8400-...
```

Response shape:
```json
{
  "run_id": "550e8400-...",
  "filename": "accounts.pdf",
  "standard": "frs105",
  "status": "complete",
  "summary": { "total": 98, "met": 61, "partially_met": 12, "missing": 18, "not_applicable": 7 },
  "metadata": { "pages": 14, "token_estimate": 12400 },
  "items": [
    {
      "item_id": "1.01.a",
      "requirement": "the part of the United Kingdom in which the micro-entity is registered",
      "status": "met",
      "evidence": "Registered in England and Wales.",
      "reasoning": "The jurisdiction of registration is stated in the company information note.",
      "confidence": 0.95,
      "reviewer_changed": 0,
      "human_override": null,
      "human_notes": null
    }
  ]
}
```

---

## Analysis Pipeline

```
PDF upload
  │
  ▼ extractor.py       Extract text page-by-page via pdfplumber
  │                    Tables converted to markdown; full text assembled with PAGE N markers
  ▼ redactor.py        Pass 1: regex (registration numbers, postcodes, email, phone)
  │                    Pass 2: spaCy en_core_web_sm NER (PERSON, ORG, GPE)
  ▼ assessor.py        Load checklist items from JSON
  │                    Batch into groups of 12, call GPT-4o once per batch
  │                    Prompt provides full redacted document + batch of items
  │                    Returns: status, evidence, reasoning, confidence per item
  ▼ reviewer.py        Second pass with GPT-4o-mini
  │                    Reviews each assessment and flags N/A vs Missing errors
  │                    Sets reviewer_changed=1 if it corrects the initial assessment
  ▼ orchestrator.py    Persists results to SQLite (backend/data/sparkz.db)
  │                    Updates in-memory progress dict for SSE streaming
  ▼ Results API
```

---

## Checklist Scripts

### XLS → JSON converter

Run once (or after updating the XLS source files):

```bash
cd backend/
python scripts/convert_checklists.py
```

Reads `../FRS105_DC_2025.xlsx` and `../FRS1021A_DC_2025.xlsx` (relative to `backend/`).
Writes `app/checklists/frs105.json` and `app/checklists/frs102.json`.

### Guidance enrichment

Run after converting checklists to populate `guidance` and `applicability_hint` fields:

```bash
python scripts/enrich_checklists.py --dry-run   # preview without API calls
python scripts/enrich_checklists.py             # enrich both standards (~$0.40)
python scripts/enrich_checklists.py --standard frs105   # one standard only
python scripts/enrich_checklists.py --force     # re-enrich already-populated items
```

See `NEXT_STEPS.md` in the project root for known issues with the checklists that should
be addressed before running enrichment.

---

## Known Limitations

- **Text PDFs only.** Scanned (image-only) PDFs will produce an empty or near-empty analysis.
  OCR support is not yet implemented.
- **Single-user / local only.** The in-memory progress dict is not shared across processes.
  Deploying multiple workers requires replacing it with Redis or a DB-backed queue.
- **No authentication.** All endpoints are public. Add auth before any shared deployment.
- **SQLite.** Fine for local use and MVP; replace with PostgreSQL for multi-user production.
- **Checklist data quality.** See `NEXT_STEPS.md` for known issues with parent/header items,
  entity-type applicability hints, and note items that should be excluded from assessment.
