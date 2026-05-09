<!--
=============================================================
Created: 2026-05-09
Author:  void
Purpose: Top-level project overview, install + usage how-to,
         and a technical description of the ingest pipeline,
         storage layout, and MCP surface for the claude-brain
         tool.
=============================================================
-->

# claude-brain

```
.-. .-')  _  .-')     ('-.                  .-') _
\  ( OO )( \( -O )   ( OO ).-.             ( OO ) )
 ;-----.\ ,------.   / . --. /  ,-.-') ,--./ ,--,'    ____  .---. .-----. .--------. .----.     .----.    .---.
 | .-.  | |   /`. '  | \-.  \   |  |OO)|   \ |  |\  .' __ \/_   |/ ,-.   \|   __   '/  ..  \   /  ..  \  /_   |
 | '-' /_)|  /  | |.-'-'  |  |  |  |  \|    \|  | )/ .'  \ ||   |'-'  |  |`--' .  /.  /  \  . .  /  \  .  |   |
 | .-. `. |  |_.' | \| |_.'  |  |  |(_/|  .     |/ | | (_/ ||   |   .'  /     /  / |  |  '  | |  |  '  |  |   |
 | |  \  ||  .  '.'  |  .-.  | ,|  |_.'|  |\    |  \ `.__.'\|   | .'  /__    .  /  '  \  /  ' '  \  /  '  |   |
 | '--'  /|  |\  \   |  | |  |(_|  |   |  | \   |   `.___ .'|   ||       |  /  /.-. \  `'  /.-.\  `'  /.-.|   |
 `------' `--' '--'  `--' `--'  `--'   `--'  `--'           `---'`-------' `--' `-'  `---'' `-' `---'' `-'`---'
```

Local CLI + MCP server for indexing docs into searchable knowledge bases ("brains") that Claude Code can query. Crawls web pages or GitHub repos, embeds with sentence-transformers, stores in ChromaDB, exposes the result over MCP. ~1150 lines of Python. Runs entirely on your machine. No SaaS, no Docker, no GPU.

## Contents

- [Quickstart](#quickstart)
- [What it does](#what-it-does)
- [Install](#install)
- [Usage](#usage)
- [CLI reference](#cli-reference)
- [MCP tools](#mcp-tools)
- [Internals](#internals)
- [Limitations](#limitations)
- [Repo layout](#repo-layout)
- [License](#license)

## Quickstart

System prereqs (pick your distro):

<details>
<summary><strong>Debian / Ubuntu / Mint / PopOS</strong></summary>

```bash
sudo apt update && sudo apt install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh

# Optional, only if you'll use --browser mode (Chromium):
sudo apt install -y libnss3 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
                    libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
                    libpango-1.0-0 libcairo2 libasound2 libgtk-3-0
```
</details>

<details>
<summary><strong>Arch / CachyOS / Manjaro / EndeavourOS</strong></summary>

```bash
sudo pacman -S --needed git curl
curl -LsSf https://astral.sh/uv/install.sh | sh

# Optional, only if you'll use --browser mode (Chromium):
paru -S --needed nss atk at-spi2-atk libcups alsa-lib gtk3
```
</details>

Plus the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code/setup) for MCP registration (skip if you'll only drive the brain from the CLI).

Clone and bootstrap:

```sh
git clone https://github.com/<you>/claude-brain.git
cd claude-brain
./bootstrap.sh
```

Activate the venv:

| Shell | Command |
|---|---|
| fish | `source .venv/bin/activate.fish` |
| bash | `source .venv/bin/activate` |
| zsh  | `source .venv/bin/activate` |

Register the MCP server, then crawl your first brain:

```sh
claude mcp add localbrain --scope user -- "$PWD/.venv/bin/python" "$PWD/server.py"
python ingest.py https://docs.example.com/ --brain example-docs --depth 6 --max-pages 800
```

In a fresh Claude Code session, ask it to search the `example-docs` brain. Claude calls the MCP tool, gets chunks with source URLs, answers grounded in your indexed docs.

## What it does

For each ingest:

1. **Fetch.** Parallel HTTP via `crawl4ai`'s `AsyncHTTPCrawlerStrategy` (default, ~150 ms/page), or Chromium with `--browser`, or raw markdown from `raw.githubusercontent.com` with `--github-docs OWNER/REPO`.
2. **Strip chrome.** `DefaultMarkdownGenerator` + `PruningContentFilter(threshold=0.48)` emit `fit_markdown`. Typically 1–10 KB of content from a 100+ KB nav-heavy page.
3. **Chunk.** `RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)`.
4. **Embed.** `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~80 MB, CPU).
5. **Persist.** ChromaDB `PersistentClient` at `./chroma_db/`. Chunk IDs are `sha1(f"{url}#{idx}")` so re-crawls upsert cleanly.
6. **Log.** Each URL attempt appends one JSON line to `crawl_log.<brain>.jsonl` (`stored` / `failed` / `empty`). `--status`, `--retry-failed`, and `--coverage` read this.

For each query:

- `search_knowledge(query, k, brain)` runs ChromaDB cosine similarity, returns top-k chunks plus their source URLs.
- `grep_brain(needle, brain, regex)` is a paged literal/regex scan. Vector search returns near-matches even when a literal isn't present, which is enough for a model to confabulate identifiers; grep gives a deterministic yes/no.
- `learn_url`, `learn_site`, `learn_github_docs`, `list_brains` mirror the CLI as MCP tools.

All URL-accepting entrypoints call `_check_url_safe` first. It rejects non-http(s) schemes and any host that resolves to a private, loopback, or link-local IP. Bypass with `LOCALBRAIN_ALLOW_PRIVATE=1` for legitimate internal docs.

## Install

The Quickstart commands are the happy path. This section is the deeper reference.

### Prerequisites

- **`uv`** for Python/venv management. [Install](https://docs.astral.sh/uv/getting-started/installation/). Auto-fetches Python 3.12 if missing.
- **Claude Code CLI** for `claude mcp add`. [Install](https://docs.claude.com/en/docs/claude-code/setup). Skip if you only want CLI use.
- **HuggingFace Hub reachable on first ingest.** The embedding model auto-downloads (~80 MB) into `~/.cache/huggingface/`. Subsequent runs are offline.
- **Chromium system libs (Linux)** only matter for `--browser` mode. `playwright install-deps` only knows `apt-get`, so non-Debian users install via the native package manager. Skip entirely if you'll use HTTP and `--github-docs` only.

### What `bootstrap.sh` does

`uv venv --python 3.12`, installs `mcp[cli] crawl4ai chromadb langchain-text-splitters sentence-transformers`, then `playwright install chromium`. Total disk after first ingest: ~600 MB.

### Setting env vars per shell

`claude-brain` honors `LOCALBRAIN_ALLOW_PRIVATE` (bypass SSRF guard) and `GITHUB_TOKEN` (lift `--github-docs` from 60 to 5000 req/h).

One-shot:
```fish
# fish
env LOCALBRAIN_ALLOW_PRIVATE=1 python ingest.py https://docs.internal/ --brain internal
```
```bash
# bash / zsh
LOCALBRAIN_ALLOW_PRIVATE=1 python ingest.py https://docs.internal/ --brain internal
```

Session-wide:
```fish
# fish
set -gx GITHUB_TOKEN ghp_xxx
```
```bash
# bash / zsh
export GITHUB_TOKEN=ghp_xxx
```

Persistent: add the lines to `~/.config/fish/config.fish` or `~/.bashrc` / `~/.zshrc`.

## Usage

### Static docs site (the common case)

```sh
python ingest.py https://docs.example.com/ \
    --brain example-docs --depth 6 --max-pages 800
```

Parallel HTTP BFS, 16 concurrent fetches per level (`--workers 16`), `depth-1` hops out from the root, capped at 800 pages. Each level batches all new chunks into one `coll.upsert()`. A 1000-page Hugo or MkDocs site finishes in 3–5 minutes.

Multiple roots:
```sh
python ingest.py https://docs.foo.com/ https://docs.bar.com/ \
    --brain my-stack --depth 4 --max-pages 500
```

Skip dead-end URL families (fnmatch, repeatable):
```sh
python ingest.py https://docs.example.com/ --brain example-docs \
    --depth 6 --max-pages 800 \
    --skip '*.yaml' --skip '*/sla/*' --skip '*/_schemas/*'
```

### JS-rendered docs site

If a fetch returns near-empty markdown but the page renders fine in a browser, the site is JS-rendered. Two options:

**Option A: pull the source repo on GitHub.** Most dev-tool docs sites (Terraform providers, MkDocs Material, Hugo with markdown content) keep the markdown in a public repo:

```sh
python ingest.py --github-docs exoscale/terraform-provider-exoscale \
    --brain terraform-exoscale
```

Hits `api.github.com/repos/{repo}/git/trees/{ref}?recursive=1` once, then fetches each `.md` via `raw.githubusercontent.com`. Default branch auto-detected. Append `@REF` to pin. Set `GITHUB_TOKEN` for higher rate limits.

**Option B: render with Chromium.** When no public source exists:
```sh
python ingest.py https://docs.example.com/ --brain example-docs \
    --depth 4 --max-pages 200 --browser
```

Slower, and may still under-collect on SPAs that lazy-load via API after initial render.

### Coverage check

`sitemap.xml` is the closest thing to ground truth for "what pages exist":

```sh
python ingest.py --coverage https://docs.example.com/ --brain example-docs
```

Reports total sitemap URLs, `stored / failed / missing` against the brain, plus the first 20 missing. Handles sitemap indexes and `.xml.gz`. `--ingest-missing URL` reports then ingests the gap at depth=1.

### Resume / retry

The manifest persists across runs:
```sh
python ingest.py --status --brain example-docs       # summary + first 10 failed URLs
python ingest.py --retry-failed --brain example-docs # re-fetch every failed/empty URL
```

Killed crawls don't lose work. Chunks upsert per BFS level. Re-run the original command and skip-known handles the resume.

### Query from Claude Code

Open a fresh session and prompt naturally, e.g. *list_brains, then search the example-docs brain for how to configure X*. Claude calls the tools and answers with citations.

### Other operations

```sh
python ingest.py --list                                  # all brains + chunk counts
python ingest.py --grep 'literal-string' --brain NAME    # paged literal scan
python ingest.py --grep '[a-z]+-pattern' --regex --brain NAME
python ingest.py --reset --brain NAME                    # drop a brain
python ingest.py URL --depth 4 --max-pages 200 --force --brain NAME
```

## CLI reference

```
ingest.py [URL ...]
          [--brain NAME] [--depth N] [--max-pages M] [--workers N]
          [--skip 'PATTERN' [--skip 'PATTERN' ...]]
          [--browser] [--force]
          [--reset] [--list] [--status]
          [--grep NEEDLE [--regex] [--grep-limit N]]
          [--coverage URL | --ingest-missing URL]
          [--retry-failed]
          [--github-docs OWNER/REPO[@REF] [--github-docs-path PREFIX]]
```

| Flag | Effect |
|---|---|
| `URL [URL ...]` | Root URL(s) when `--depth>1`, single pages when `--depth=1`. |
| `--brain NAME` | Chroma collection / manifest to read/write. Default `knowledge`. |
| `--depth N` | `1` = single page (default). `2+` = BFS, `N-1` hops out. |
| `--max-pages M` | Hard cap per root, default 50. Counts fetched, not stored. |
| `--workers N` | Parallel fetches per BFS level. Default 16. |
| `--skip PATTERN` | fnmatch URL pattern to drop from the BFS. Repeatable. |
| `--browser` | Use Chromium instead of `AsyncHTTPCrawlerStrategy`. |
| `--force` | Ignore skip-known, re-fetch + `_purge_url` + upsert. |
| `--reset` | Drop the brain's collection. Combinable with a URL crawl. |
| `--list` | Print every brain + chunk counts. |
| `--status` | Manifest summary. |
| `--grep NEEDLE [--regex]` | Paged literal/regex scan over all chunks. |
| `--grep-limit N` | Cap matches printed (default 20). |
| `--coverage URL` | Diff brain against `sitemap.xml`. |
| `--ingest-missing URL` | Coverage + ingest the gap. |
| `--retry-failed` | Re-crawl every URL whose latest entry is failed/empty. |
| `--github-docs OWNER/REPO[@REF]` | Enumerate `.md` via GitHub tree API, fetch via raw.githubusercontent.com. |
| `--github-docs-path PREFIX` | Path prefix in the repo (default `docs/`; `""` = all `.md`). |

## MCP tools

| Tool | Signature | Purpose |
|---|---|---|
| `list_brains` | `() → str` | Brains + chunk counts. |
| `search_knowledge` | `(query, k=5, brain)` | Vector cosine search. |
| `grep_brain` | `(needle, brain, regex=False, limit=20)` | Literal/regex scan. Deterministic existence check. |
| `learn_url` | `(url, force=False, browser=False, brain)` | Single-URL ingest. |
| `learn_site` | `(url, depth=2, max_pages=30, workers=16, force=False, browser=False, skip=None, brain)` | BFS-crawl a site. |
| `learn_github_docs` | `(repo, brain, path_prefix="docs/", ref="", force=False)` | Pull `.md` from a GitHub repo. |

`search_knowledge` and `grep_brain` are intentionally redundant. Vector search is fast and good for "approximately about X". Grep is the only honest answer to "is this exact string present?".

## Internals

```
URLs / GitHub repo
        │
        ▼
 ┌─────────────────────────────────────────┐
 │ Fetcher                                 │
 │   AsyncHTTPCrawlerStrategy   (default)  │  parallel via MemoryAdaptiveDispatcher
 │   AsyncWebCrawler + Chromium (--browser)│  serial through Playwright
 │   raw.githubusercontent.com  (--github-)│  urllib direct
 │   _check_url_safe gate on every path    │  scheme + DNS + ipaddress.is_private
 └─────────────────────────────────────────┘
        │ HTML, or markdown verbatim
        ▼
 ┌─────────────────────────────────────────┐
 │ DefaultMarkdownGenerator                │
 │   + PruningContentFilter (0.48 fixed)   │  → fit_markdown
 │   (skipped for --github-docs)           │  source is already markdown
 └─────────────────────────────────────────┘
        │ clean markdown
        ▼
 ┌─────────────────────────────────────────┐
 │ RecursiveCharacterTextSplitter          │  chunk_size=1000, overlap=100
 └─────────────────────────────────────────┘
        │ list[chunk]
        ▼
 ┌─────────────────────────────────────────┐
 │ SentenceTransformerEmbeddingFunction    │  all-MiniLM-L6-v2, 384-dim, CPU
 │   batched per BFS level                 │  one upsert per level
 └─────────────────────────────────────────┘
        │ list[(id=sha1(url#i), text, embedding, meta)]
        ▼
 ┌─────────────────────────────────────────┐
 │ ChromaDB PersistentClient               │  ./chroma_db/  (SQLite + parquet)
 │   one collection per brain              │
 │   functools.lru_cache singleton         │  one open SQLite per process
 └─────────────────────────────────────────┘
        ▲
        │ similarity_search / .get / .query
        │
 ┌─────────────────────────────────────────┐
 │ FastMCP server (server.py)              │  stdio, six tools
 └─────────────────────────────────────────┘
        ▲
        │
   Claude Code (or any MCP client)
```

### BFS

Custom level-by-level BFS instead of `BFSDeepCrawlStrategy`, so each level pushes through `arun_many` + `MemoryAdaptiveDispatcher(max_session_permit=workers)`. Per level:

1. Filter the frontier: drop visited URLs and `--skip` matches.
2. Leaf level (`level == depth - 1`): also drop URLs already in `known_sources`. No fetch, no embed.
3. Intermediate levels: keep known URLs in the wave for link discovery; embedding is gated on `force or norm not in known` so we never re-embed stored content.
4. `arun_many` parallel-fetches the wave.
5. Per result, accumulate `_prepare_chunks` output into a level-wide `pending`.
6. After the wave, `_bulk_upsert` runs `pending` through the embedder in 256-chunk batches with progress prints. One torch graph per level instead of per page.
7. Internal links (same host, http/s) get appended to `next_frontier` after dedup.

### Chroma client lifetime

`get_collection(brain)` is `@functools.lru_cache(maxsize=None)`. `_client()` and `_embed_fn()` are `lru_cache(maxsize=1)`. One `PersistentClient` and one embedding model per process. First call ~3 s (model load), every subsequent call cache-hits.

### Manifest

```json
{"url":"…","status":"stored","chunks":7,"ts":"2026-05-09T09:00:55+00:00"}
{"url":"…","status":"failed","error":"HTTP 404 at …","ts":"…"}
{"url":"…","status":"empty","reason":"no content after pruning","ts":"…"}
```

Append-only. `--status` and `--retry-failed` reduce by latest-per-URL via `_latest_per_url`, so a `failed → stored` URL shows as `stored`.

### Skip-known reachability

If a URL is skipped at level N-1, the BFS never reads its outgoing links, so children only reachable through it stay undiscovered. Trade-off for fast resumes. Use `--force` to re-explore.

### Paged `coll.get()`

ChromaDB's unfiltered `get()` builds a SQL query that hits SQLite's bind-variable cap (~999) past ~10k records. `known_sources()` and `grep_brain()` page with `limit=1000` + `offset`.

### URL safety

`_check_url_safe(url)`:
- Reject non-http(s) schemes.
- `socket.getaddrinfo` the host, then `ipaddress.ip_address(addr)` per result.
- Reject if any address is `is_private`, `is_loopback`, `is_link_local`, or `is_reserved` (catches RFC1918, `127.0.0.0/8`, `169.254.0.0/16`, IPv6 equivalents).
- `LOCALBRAIN_ALLOW_PRIVATE=1` bypasses for internal docs.

### Embedding model coupling

Embeddings encode which model produced them. `EMBED_MODEL` is fixed at module level. Different brains can in principle use different models, but you'd fork `EMBED_MODEL` per brain. `all-MiniLM-L6-v2` is a sensible default for English docs.

## Limitations

- CPU-only embedding. ~100 chunks/s on a modern laptop. Install a CUDA/ROCm torch and sentence-transformers picks it up automatically.
- No cross-encoder re-ranker.
- No auth or multi-user. Run Chroma in server mode if you want shared access.
- English-tuned embeddings. Swap `paraphrase-multilingual-MiniLM-L12-v2` and re-ingest for other languages.
- No automatic re-crawl on doc updates. Re-run `python ingest.py URL ...`; skip-known short-circuits unchanged pages, `--force` overrides.
- BFS depends on `<a href>` in static HTML. SPAs need `--browser` or `--github-docs`.

## Repo layout

```
claude-brain/
├── ingest.py                # CLI + ingest pipeline + helpers
├── server.py                # FastMCP server
├── bootstrap.sh             # one-shot uv-based setup
├── README.md
├── LICENSE                  # MIT
├── .gitignore               # ignores chroma_db/, manifests, .venv, settings.local.json
├── chroma_db/               # generated, gitignored: vector store
└── crawl_log.<brain>.jsonl  # generated, gitignored: append-only manifest per brain
```

## License

[MIT](./LICENSE).
