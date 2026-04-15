# 🏛️ Federal Reserve AI Research Assistant

**A production-grade Retrieval-Augmented Generation (RAG) application that puts 10,000+ pages of Federal Reserve documents at your fingertips — powered by Snowflake Cortex Search, Mistral Large 2, and Streamlit.**

🔗 **Live Demo:** [federal-reserve-ai-research-assistant.streamlit.app](https://federal-reserve-ai-research-assistant.streamlit.app/)

---

## Overview

Monetary policy research requires sifting through dense, sprawling documents — FOMC meeting minutes, press conference transcripts, Beige Books, Monetary Policy Reports, and more — spread across years and dozens of PDFs. This project eliminates that friction.

The Federal Reserve AI Research Assistant is a conversational research tool that lets economists, students, investors, and policy analysts ask plain-English questions and receive grounded, source-cited answers synthesized directly from the Fed's own published documents (2023–2026). Every response is traceable to its source, and users can download their full research session as a formatted PDF.

---

## Architecture

```
User Query
    │
    ▼
Streamlit UI (app.py)
    │
    ├──► Snowflake Cortex Search (FOMC_SEARCH_SERVICE)
    │        └── Embedding Model: snowflake-arctic-embed-l-v2.0
    │        └── Semantic search over ~10,000 pages of Fed PDFs
    │
    ├──► Context Assembly & Prompt Engineering
    │        └── Year-aware filtering & deduplication
    │        └── XML-tagged system prompt with glossary + instructions
    │
    └──► Snowflake Cortex Complete (mistral-large2)
             └── Fallback: mixtral-8x7b
             └── Grounded, cited natural language response
```

**Key infrastructure:**
- **Snowflake Cortex Search** — fully managed vector search service with sub-second semantic retrieval
- **Snowflake Cortex Complete** — LLM inference via SQL (`SNOWFLAKE.CORTEX.COMPLETE`), bypassing REST auth issues entirely
- **Snowflake Snowpark** — Python-native session management and DataFrame API
- **Streamlit** — interactive frontend deployed on Streamlit Community Cloud
- **TruLens** — RAG observability and evaluation pipeline (Notebook)
- **ReportLab** — programmatic PDF generation for research export

---

## Features

- **Conversational Q&A over Fed documents** — ask about rate decisions, inflation projections, dot plots, labor markets, financial stability risks, tariff impacts, and more
- **Source transparency** — every response links back to the exact Fed PDF (FOMC Minutes, Beige Book, Press Conference transcripts, Projection Tables, Monetary Policy Reports, Financial Stability Reports)
- **Year-aware retrieval** — queries mentioning specific years (e.g. "mid-2023") are automatically scoped to the relevant document window
- **Conversation memory** — maintains rolling context of the last 2 Q&A turns, with semantic weighting toward follow-up questions
- **Animated streaming UX** — a threaded architecture runs LLM inference in the background while a live progress bar keeps the UI responsive
- **Fallback model routing** — automatically falls back from `mistral-large2` to `mixtral-8x7b` on failure
- **PDF research export** — download the full session with all questions, answers, and cited sources as a timestamped PDF
- **13 curated example questions** in the sidebar covering policy, labor, inflation, tariffs, financial stability, and historical FOMC analysis

---

## RAG Pipeline — Technical Details

### 1. Data Ingestion (Jupyter Notebook)
- Federal Reserve PDFs downloaded directly from `federalreserve.gov`
- Uploaded to a Snowflake internal stage (`@cortex_search_tutorial_db.public.fomc`)
- Parsed via `SNOWFLAKE.CORTEX.PARSE_DOCUMENT` in `LAYOUT` mode for structure-aware text extraction
- Chunked with `SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER` (chunk size: 1800 tokens, overlap: 250)
- Indexed into `FOMC_SEARCH_SERVICE` using the `snowflake-arctic-embed-l-v2.0` embedding model

### 2. Retrieval
- `CortexSearchRetriever` issues queries against the Cortex Search service, fetching up to 36 candidate chunks (3× the final limit), then deduplicates by source document
- Year extraction via regex identifies temporal scope in the user's query and filters results accordingly
- Final context is sorted by recency, capped at 12 documents

### 3. Prompt Engineering
- Structured system prompt uses XML tags (`<context>`, `<instructions>`, `<glossary>`, `<conversation_history>`) for reliable instruction following
- Context is grouped by year for chronological coherence
- Prompt is hard-capped at 40,000 characters to stay within the model's context window
- The model is instructed to cite source documents inline (e.g., *"According to the FOMC Minutes from January 2025..."*) and to flag when it's reasoning beyond the provided context

### 4. Observability (TruLens)
- RAG pipeline instrumented with OpenTelemetry spans via TruLens `@instrument` decorators
- Retrieval and generation spans are logged to a dedicated Snowflake observability database
- Enables evaluation of context relevance, answer faithfulness, and retrieval quality

---

## Document Coverage

| Document Type | Description |
|---|---|
| FOMC Minutes | Detailed records of each Federal Open Market Committee meeting |
| Press Conference Transcripts | Chair Powell's post-meeting Q&A sessions |
| Beige Book | Regional economic condition summaries (8× per year) |
| Monetary Policy Reports | Semi-annual reports to Congress |
| Projection Tables (Dot Plot) | Rate, inflation, and GDP projections by FOMC participants |
| FOMC Longer-Run Goals | The Fed's formal statement on monetary policy framework |
| Financial Stability Reports | Semi-annual assessments of systemic financial risk |

**Coverage period: 2023 – 2026**

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Streamlit |
| Cloud Data Platform | Snowflake |
| Vector Search | Snowflake Cortex Search |
| Embeddings | snowflake-arctic-embed-l-v2.0 |
| LLM (primary) | Mistral Large 2 (via Snowflake Cortex) |
| LLM (fallback) | Mixtral 8x7B (via Snowflake Cortex) |
| Python SDK | Snowpark Python, snowflake-ml-python |
| RAG Observability | TruLens + OpenTelemetry |
| PDF Generation | ReportLab |
| Deployment | Streamlit Community Cloud |

---

## Local Setup

### Prerequisites
- Python 3.10+
- A Snowflake account with Cortex features enabled
- Streamlit secrets configured (see below)

### Installation

```bash
git clone https://github.com/comet000/federal-reserve-ai-research-assistant.git
cd federal-reserve-ai-research-assistant
pip install -r requirements.txt
```

### Configure Secrets

Create `.streamlit/secrets.toml`:

```toml
account   = "your-snowflake-account"
user      = "your-username"
password  = "your-password"
warehouse = "CORTEX_SEARCH_TUTORIAL_WH"
database  = "CORTEX_SEARCH_TUTORIAL_DB"
schema    = "PUBLIC"
role      = "your-role"
```

### Run

```bash
streamlit run app.py
```

### Build the RAG Index

Open `BUILD_RAG_WITH_CORTEX_SEARCH.ipynb` in Snowflake Notebooks and follow the step-by-step instructions to:
1. Create the database, schema, and warehouse
2. Upload FOMC PDFs to a Snowflake stage
3. Parse, chunk, and index documents into the Cortex Search service
4. Evaluate the pipeline with TruLens

---

## Example Questions

> *"What's the median rate projection for next year?"*

> *"To what extent do tariff policy and trade disruptions factor into the Fed's inflation outlook?"*

> *"How did the FOMC assess the labor market in mid-2024?"*

> *"What are the greatest risks to financial stability over the next 12–18 months?"*

> *"When and how fast should the Fed cut rates — if at all?"*

> *"Are supply chain issues still showing up regionally?"*

---

## Project Structure

```
federal-reserve-ai-research-assistant/
├── app.py                                  # Main Streamlit application
├── BUILD_RAG_WITH_CORTEX_SEARCH.ipynb      # Data pipeline + TruLens evaluation notebook
├── requirements.txt                        # Python dependencies
└── .streamlit/
    └── secrets.toml                        # Snowflake credentials (not committed)
```

---

## Design Decisions & Engineering Notes

**Why Snowflake Cortex instead of a standalone vector DB?** All data, compute, and LLM inference live within a single Snowflake environment. This eliminates egress costs, simplifies auth, and keeps PII and sensitive financial research within a governed data platform.

**Why call Cortex Complete via SQL instead of the REST API?** Snowflake's Cortex REST endpoint has auth friction in certain deployment environments. Executing `SNOWFLAKE.CORTEX.COMPLETE` as a SQL function via Snowpark is simpler, more reliable, and avoids token management entirely.

**Why thread the LLM call?** Blocking Streamlit on a synchronous SQL call freezes the UI. A background thread runs the inference while the main thread animates a progress bar, giving users real-time feedback on a process that can take 10–30 seconds.

**Why XML-tagged prompts?** Structured tags (`<context>`, `<instructions>`, `<glossary>`) give the model unambiguous section boundaries, which improves instruction following compared to plain-text prompts — especially for a large, multi-section prompt with up to 40,000 characters of context.

---

## License

MIT License — free to use, modify, and build upon.

---

*Built as an independent project exploring the intersection of large language models, enterprise data infrastructure, and applied economic research.*
