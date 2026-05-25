# PIIExtractor (standalone)

Local Flask toolkit for scripted OpenAI PII-span extraction plus an offline deterministic
scanner in the browser. Examples live in **`fixtures/email_ex.json`**.

This folder is a **standalone copy**: it bundles `_shared/openai_api_key_resolver.py`
and `_shared/aws_clients.py` for key resolution so it does **not** depend on the rest
of the monorepo’s `genai/` tree.

## Run

```bash
cd PIIExtractor
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5011/

### OpenAI key

- **`OPENAI_API_KEY`** — export directly, or  
- **`PIIExtractor/.env`** — merged automatically (does not override already-set env vars),
  or set **`GENAI_DOTENV_PATH`** to another file.
- Optional AWS-backed key resolution ( **`REGION`** + S3 / SSM env vars**) uses **`_shared/aws_clients.py`** the same way as other GenAI toolkits.

## Layout

| Region | Behaviour |
|--------|-----------|
| Left | Example entries from fixture JSON (`subject` as title, `id` subtitle). |
| Center | Unicode body with `<mark>` highlights: breakpoints include **all** spans per pipeline, including overlaps; spans touched by **both** pipelines get a green outline; spans from **only one** pipeline get a yellow accent. When `[start,end)` does not match `info_value`, the UI snaps the span via a bounded `info_value` search near those offsets before rendering. |
| Center header | **Run extraction** (OpenAI / async logs) and **Run deterministic** (offline scan). |
| Center (below body) | Collapsible **Extraction logs** while the agent job runs. |
| Center (details) | Collapsible **Details**: tiktoken prompt estimates plus usage after an **agent** run. |
| Right | **Agent** and **Deterministic** panes with **Clear** per pane (drops cache for selected example only). |
| Persisted caches | **`local_cache/*.json`** via **`GET`/`PUT`** `/api/cache/pii-stores`; gitignored — treat as sensitive. |

## API

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/examples` | Current fixture rows. |
| `POST` | `/api/extract` | `{ "example_id": "ex-1" }` → `{ ok, job_id }` async job; `missing_openai_api_key` → 503. |
| `GET` | `/api/extract/progress?job_id=…` | Poll agent job; **result** includes extractions + usage when complete. |
| `GET` | `/api/cache/pii-stores` | Sidebar disk caches + `cache_dir`. |
| `PUT` | `/api/cache/pii-stores` | `{ agent_store?, deterministic_store? }` whole-map writes. |

## Environment

| Variable | Meaning |
|----------|---------|
| `GENAI_DOTENV_PATH` | Optional `.env` path (**default**: `PIIExtractor/.env`) |
| `OPENAI_API_KEY` | Preferred local key |
| `OPENAI_API_KEY_PARAM_NAME` + `REGION` | SSM parameter |
| `OPENAI_API_KEY_S3_URI` or bucket+key + `REGION` | S3 key object |
| `EMAIL_PII_LAB_EMAIL_EX_JSON` | Override path to fixtures JSON |
| `EMAIL_PII_LAB_MODEL` | Model (default `gpt-5-mini`) |
| `EMAIL_PII_LAB_REASONING_EFFORT` | `low` \| `medium` \| `high` (default **`low`**); empty to omit |
| `EMAIL_PII_LAB_TOKEN_MODEL` | Tiktoken hint (defaults to `EMAIL_PII_LAB_MODEL`) |

## Modules

Core extraction logic: **`services/email_extract.py`**  
User-visible strings: **`content/ui_strings.json`**  
Deterministic scan: **`static/js/deterministicPiiScan.js`**
