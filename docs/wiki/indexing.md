# Indexing pipeline

## Overview
The ingest pipeline turns a domain's Markdown into a searchable embedding store. `indexer.py` orchestrates it; the `engine/` modules do the work: `chunk` splits pages, `embed` calls the embeddings API, `store` quantizes and persists records, and `config` loads tuning from the environment. `indexer.py` also appends the ingest log read by [[authoring-and-linting#Health linting]]. Called by [[mcp-server#Write path]] and `wiki_index`.

## Index domain
`index_domain(cfg, base, domain)` rebuilds one domain incrementally. It loads existing records keyed by `id#chunk`, gathers `*.md` (skipping `.iwiki`), and chunks each file. A chunk is reused when its `hash` and `dim` match the stored record; only changed chunks are embedded, which keeps API calls minimal. Records are sorted by `(file, heading, chunk)` and saved. It returns `indexed_chunks`, `reused`, `embedded`, `bytes`, and `over_cap` (true past the 8 MB `CAP_BYTES`).

## Markdown chunking
`chunk_markdown` splits content on `##` headings into sections. The first `## Overview` section is the article summary and is itself excluded from the index. Every other section's body is word-split into overlapping sub-chunks, each prefixed with the page `# Title`, the article summary, the `## heading`, and the section lead (capped at `LEAD_MAX` 250). This makes each vector carry whole-article and whole-section context. The chunk `hash` is `sha256(text)[:16]`.

## Embeddings client
`embed_texts(cfg, texts)` posts to an OpenAI-compatible `{base_url}/embeddings`, sending `model`, `input`, and `dimensions`, with a `Bearer` key. It returns one vector per input, ordered by the response `index`. Transient failures — timeouts, transport errors, HTTP 5xx — are retried up to three times with exponential backoff; persistent failure raises `EmbedError`, which [[mcp-server#Error handling]] surfaces as a `HALT:`.

## Vector store
`store.py` is the JSONL persistence seam. Each `Record` holds `id`, `file`, `heading`, `chunk`, `hash`, `dim`, plus an int8-quantized vector: `quantize` scales by `peak/127`, `dequantize` reverses it. This shrinks the index roughly 4× versus float32. `load_index`/`save_index` read and write JSONL; `VectorStore` wraps a single index path so callers depend only on `load`/`save`/`query`, easing a later SQLite/sqlite-vec swap.

## Configuration
`Config.load()` reads all tuning from the environment and is the stop-rule gate: it raises `ConfigError` unless both `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY` are set. Defaults: model `text-embedding-3-small`, `dimensions` 1536, `chunk_size` 512, `chunk_overlap` 64, `summary_max` 400, `top_k` 8, `score_threshold` 0.2, `graph_depth` 2. An optional `.iwikiignore` (gitignore syntax) compiles to a `PathSpec` only when `load_ignore` is requested.

## Ingest log
`append_log` records one JSON line per ingest in `<domain>/.iwiki/log.jsonl` with keys `op`, `source`, `page`, `date`, and `src_hash` (`sha256[:16]` of the source, via `src_hash`). The log powers staleness detection: [[authoring-and-linting#Health linting]] compares the stored `src_hash` against the live source to flag pages whose source changed after their last ingest.
