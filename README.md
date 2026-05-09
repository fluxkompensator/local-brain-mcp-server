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

A local CLI + MCP server that turns web docs and GitHub repos into searchable vector knowledge bases ("brains") for Claude Code or any other MCP client. ~1150 LoC of Python. **Everything runs on your machine** — fetcher, embedding model, vector store, MCP server. No SaaS, no API keys (one optional GitHub token for high-volume crawls), no Docker, no GPU. One `bootstrap.sh` from a fresh clone to a working setup.

---

## What this does

For each ingest:

1. **Fetch.** Either parallel HTTP via `crawl4ai`'s `AsyncHTTPCrawlerStrategy` (default, ~150 ms/page), Chromium via `BrowserConfig` if you pass `--browser` (for JS-rendered sites), or raw markdown straight off `raw.githubusercontent.com` if you pass `--github-docs OWNER/REPO` (no HTML at all).
2. **Strip chrome.** `crawl4ai`'s `DefaultMarkdownGenerator` with `PruningContentFilter(threshold=0.48, threshold_type="fixed")` runs a heuristic prune over the rendered DOM and emits `fit_markdown` — typically 1–10 KB of actual content from a 100+ KB nav-heavy doc page.
3. **Chunk.** `langchain_text_splitters.RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)` splits on prose boundaries, falling back to characters.
4. **Embed.** `sentence-transformers/all-MiniLM-L6-v2` (~80 MB model, 384-dim float vectors) on CPU via `chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction`.
5. **Persist.** ChromaDB `PersistentClient` writes a per-brain collection at `./chroma_db/`. Each chunk gets a deterministic ID `sha1(f"{url}#{idx}")` so re-crawls upsert cleanly. URL `_normalize` strips fragments so `/page#a` and `/page#b` dedupe.
6. **Log.** Every URL attempt appends one JSON line to `crawl_log.<brain>.jsonl` with status `stored` / `failed` / `empty` and a timestamp. The manifest is what `--status`, `--retry-failed`, and `--coverage` read.

For each query (from Claude Code or any MCP client):

- `search_knowledge(query, k, brain)` — embeds the query with the same model, runs ChromaDB cosine similarity, returns the top-k chunks with their `source` URL metadata.
- `grep_brain(needle, brain, regex)` — pages through every chunk and runs Python `re` on the document text. Used when "does this exact identifier exist?" matters more than "what's similar?" Vector search returns near-matches even when the literal string is absent, which is enough for a model to confabulate an identifier; `grep_brain` gives a deterministic yes/no.
- `learn_url`, `learn_site`, `learn_github_docs`, `list_brains` — same as the CLI, accessible to Claude as MCP tools.

## Why local-first, with minimum effort

**Local.** The vector store is one directory of SQLite + binary blobs. The embedding model runs on your CPU. The MCP server is a stdio process spawned by Claude Code. Nothing leaves your machine. Backup is `tar czf brain.tar.gz chroma_db/`. Move to another machine? `scp` the tarball.

**Minimum effort.** One bootstrap script gets you from a fresh clone to a working setup; one `claude mcp add` registers the server; one CLI command per source you want indexed. Defaults are tuned for static documentation sites — most of the time you don't pass any flags beyond the URL and `--depth`. Re-runs are safe by default (skip-known short-circuits at the URL level), so killed crawls resume cleanly. `--coverage URL` measures completeness against the site's own `sitemap.xml`.

**Boundary safety.** All URL-accepting entrypoints (`ingest`, `_fetch_sitemap_bytes`, the MCP `learn_*` tools) call `_check_url_safe` before any network I/O. It rejects non-`http(s)` schemes and any hostname that resolves to a private, loopback, or link-local address. Important when an MCP-exposed tool can be called by an LLM with arbitrary URLs from prompt-injected content. Bypass with `LOCALBRAIN_ALLOW_PRIVATE=1` for legitimate internal-docs crawls.

---

## Install

Tested on Linux, Python 3.12. macOS should work. Works under fish, bash, and zsh — see the activate-script note below.

### Prerequisites

- **`uv`** — Python package/venv manager. [Install](https://docs.astral.sh/uv/getting-started/installation/). uv auto-fetches Python 3.12 if it isn't already on the system.
- **Claude Code CLI** — needed by `claude mcp add` to register the server. [Install](https://docs.claude.com/en/docs/claude-code/setup). If you only intend to drive the brain from the CLI (not Claude Code), you can skip this and ignore the MCP-register step.
- **HuggingFace Hub reachable on first ingest.** The embedding model (`sentence-transformers/all-MiniLM-L6-v2`, ~80 MB) auto-downloads from `huggingface.co` into `~/.cache/huggingface/` the first time `get_collection()` runs. Strict-firewall environments need either a one-time online run or a local mirror; subsequent runs are fully offline.
- **Chromium system libs (Linux, only for `--browser` mode).** `bootstrap.sh` runs `playwright install chromium` which fetches the browser binary, but the underlying system libraries (`nss`, `atk`, `at-spi2-atk`, `cups`, `alsa-lib`, `gtk3`, …) need to be installed separately on non-Debian distros. `playwright install-deps` only knows `apt-get`, so on Arch / Fedora / Void use the native package manager. Arch example: `paru -S nss atk at-spi2-atk libcups alsa-lib gtk3`. **You can skip this entirely if you'll only ever use the default HTTP fetcher and `--github-docs`.**

```sh
git clone https://github.com/<you>/claude-brain.git
cd claude-brain
./bootstrap.sh           # uv venv + deps + chromium
```

Activate the venv for the shell you're using:

```fish
# fish
source .venv/bin/activate.fish
```
```bash
# bash
source .venv/bin/activate
```
```zsh
# zsh
source .venv/bin/activate
```

Register the MCP server with Claude Code (user scope, one-time):

```fish
# fish — $PWD works as-is
claude mcp add localbrain --scope user -- \
    $PWD/.venv/bin/python $PWD/server.py
```
```bash
# bash / zsh — same idea, double-quote $PWD if your path has spaces
claude mcp add localbrain --scope user -- \
    "$PWD/.venv/bin/python" "$PWD/server.py"
```

`bootstrap.sh` runs `uv venv --python 3.12`, installs `mcp[cli] crawl4ai chromadb langchain-text-splitters sentence-transformers`, then `playwright install chromium` (only used by `--browser`; safe to skip if you'll never use that mode). Total disk: ~600 MB for the venv + Chromium + the embedding model on first ingest.

Verify (works in all three shells):
```sh
claude mcp list | grep localbrain     # → ✓ Connected
python ingest.py --list                 # empty until you crawl something
```

### Setting env vars (shell quirks)

The two env vars `claude-brain` honors — `LOCALBRAIN_ALLOW_PRIVATE` (bypass the SSRF guard for internal docs) and `GITHUB_TOKEN` (lift `--github-docs` from 60 req/h to 5000 req/h) — set differently per shell.

**One-shot, just for one command:**
```fish
# fish — use the `env` builtin, since fish doesn't accept VAR=val cmd inline
env LOCALBRAIN_ALLOW_PRIVATE=1 python ingest.py https://docs.internal/ --brain internal-docs
env GITHUB_TOKEN=ghp_xxx python ingest.py --github-docs huge/repo --brain huge
```
```bash
# bash / zsh — inline VAR=val prefix works
LOCALBRAIN_ALLOW_PRIVATE=1 python ingest.py https://docs.internal/ --brain internal-docs
GITHUB_TOKEN=ghp_xxx python ingest.py --github-docs huge/repo --brain huge
```

**Persistent, for the whole shell session:**
```fish
# fish
set -gx LOCALBRAIN_ALLOW_PRIVATE 1
set -gx GITHUB_TOKEN ghp_xxx
```
```bash
# bash / zsh
export LOCALBRAIN_ALLOW_PRIVATE=1
export GITHUB_TOKEN=ghp_xxx
```

**Persistent across sessions:** add the `set -gx` (fish) lines to `~/.config/fish/config.fish`, or the `export` (bash/zsh) lines to `~/.bashrc` / `~/.zshrc`.

---

## How to use it

### Ingest a static docs site (the common case)

```fish
python ingest.py https://docs.example.com/ \
    --brain example-docs --depth 6 --max-pages 800
```

What happens: parallel HTTP BFS rooted at the URL, 16 concurrent fetches per BFS level (`--workers 16` by default), follows same-host links up to 5 hops out (`depth - 1`), capped at 800 pages. Each level batches all new chunks into a single `coll.upsert()` call so the embedding model sees one big tensor instead of N tiny ones. On a typical 1000-page Hugo or MkDocs site this finishes in 3–5 minutes.

You can pass multiple URLs as roots:
```fish
python ingest.py https://docs.foo.com/ https://docs.bar.com/ \
    --brain my-stack --depth 4 --max-pages 500
```

Filter dead-end URL families with repeatable `--skip` (fnmatch):
```fish
python ingest.py https://docs.example.com/ \
    --brain example-docs --depth 6 --max-pages 800 \
    --skip '*.yaml' --skip '*/sla/*' --skip '*/_schemas/*'
```

### Ingest a JS-rendered docs site

If a fetch returns near-empty markdown but the page renders fine in a browser, the site is JS-rendered. Two options, in order of preference:

**Option A — pull the source repo on GitHub.** Almost every dev-tool docs site (Terraform providers, MkDocs Material projects, Hugo with markdown content) keeps the actual markdown in a public GitHub repo. Bypass HTML entirely:

```fish
python ingest.py --github-docs exoscale/terraform-provider-exoscale \
    --brain terraform-exoscale
```

This hits `api.github.com/repos/{repo}/git/trees/{ref}?recursive=1` once to enumerate every `.md` under `docs/`, then fetches each via `raw.githubusercontent.com`. Default branch auto-detected. Append `@REF` (`exoscale/...@v0.65.0`) to pin. Set `GITHUB_TOKEN` for the 5000 req/h authenticated rate limit if you'll be ingesting big repos.

**Option B — render with Chromium.** Falls back when no GitHub source exists:
```fish
python ingest.py https://docs.example.com/ --brain example-docs \
    --depth 4 --max-pages 200 --browser
```
Slower (Chromium per page) and may still under-collect on SPAs that lazy-load content via API after page load.

### Verify completeness

`sitemap.xml` is the closest thing to ground truth for "what pages exist on this site":

```fish
python ingest.py --coverage https://docs.example.com/ --brain example-docs
```

Output: total sitemap URLs, `stored / failed / missing` breakdown against the brain, first 20 missing URLs. Handles sitemap indexes (recursively follows `<sitemap><loc>`) and `.xml.gz`. `--ingest-missing URL` runs the report and then ingests any sitemap URL not yet in the DB at depth=1.

### Resume after a kill / fix failures

The manifest persists across runs:
```fish
python ingest.py --status --brain example-docs       # summary + first 10 failed URLs
python ingest.py --retry-failed --brain example-docs   # re-fetch every failed/empty URL at depth=1, force=True
```

Killed crawls don't lose work — chunks upsert per BFS level, so whatever made it in stays. Re-run the same `python ingest.py URL ...` command and skip-known + the BFS link-discovery semantics handle the resume.

### Use the brain from Claude Code

Open a fresh session (MCP servers load at session start) and ask in plain language:

> *"Use list_brains to see what's available, then search the example-docs brain for how to configure X. If you need to verify an exact identifier, use grep_brain."*

Claude calls `list_brains`, then `search_knowledge(query="configure X", brain="example-docs")`, then optionally `grep_brain(needle="some-identifier", brain="example-docs")`. Each chunk comes with a `source` URL so answers can be cited.

### Other operations

```fish
python ingest.py --list                                      # all brains + chunk counts
python ingest.py --grep 'literal-string' --brain NAME         # paged literal scan
python ingest.py --grep '[a-z]+-pattern' --regex --brain NAME
python ingest.py --reset --brain NAME                         # drop a brain
python ingest.py URL --depth 4 --max-pages 200 --force \
    --brain NAME                                              # ignore skip-known, re-fetch + overwrite
```

---

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
| `--brain NAME` | Which Chroma collection / manifest to read/write. Default `knowledge`. |
| `--depth N` | `1` = single page (default). `2+` = BFS, `N-1` hops out. |
| `--max-pages M` | Hard cap per root, default 50. Counts fetched pages, not stored. |
| `--workers N` | Parallel fetches per BFS level via `MemoryAdaptiveDispatcher(max_session_permit=N)`. Default 16. |
| `--skip PATTERN` | fnmatch URL pattern dropped from the BFS frontier. Repeatable. |
| `--browser` | Switch fetcher from `AsyncHTTPCrawlerStrategy` to Chromium via `BrowserConfig`. |
| `--force` | Ignore skip-known, re-fetch + `_purge_url` + upsert. |
| `--reset` | Drop the brain's Chroma collection. Combinable with a URL crawl. |
| `--list` | Print every brain in `chroma_db/` with chunk counts. |
| `--status` | Manifest summary (cumulative + latest-per-URL status counts; first 10 failed). |
| `--grep NEEDLE` | Page through every chunk, return windowed excerpts containing `NEEDLE`. |
| `--regex` | Treat `NEEDLE` as a Python regex. |
| `--grep-limit N` | Cap matches printed (default 20). |
| `--coverage URL` | Diff brain against `sitemap.xml`. URL can be sitemap or site root. |
| `--ingest-missing URL` | Run `--coverage`, then ingest every sitemap URL not yet in the brain (depth=1). |
| `--retry-failed` | Re-crawl every URL whose latest manifest entry is `failed` or `empty`. |
| `--github-docs OWNER/REPO[@REF]` | Enumerate `.md` files via GitHub tree API, fetch each from `raw.githubusercontent.com`. |
| `--github-docs-path PREFIX` | Path prefix in the repo (default `docs/`; `""` matches all `.md`). |

---

## MCP tools

The FastMCP server exposes six tools, each accepting a `brain` parameter (defaults to `knowledge`):

| Tool | Signature | Purpose |
|---|---|---|
| `list_brains` | `() → str` | Enumerate brains + chunk counts. Use first when unsure which brain holds the topic. |
| `search_knowledge` | `(query, k=5, brain)` | Vector cosine search. Returns top-k chunks with `[source: url]` headers. |
| `grep_brain` | `(needle, brain, regex=False, limit=20)` | Literal/regex scan across all chunks. Deterministic existence check. |
| `learn_url` | `(url, force=False, browser=False, brain)` | Single-URL fetch + chunk + embed. |
| `learn_site` | `(url, depth=2, max_pages=30, workers=16, force=False, browser=False, skip=None, brain)` | BFS-crawl a site. |
| `learn_github_docs` | `(repo, brain, path_prefix="docs/", ref="", force=False)` | Pull `.md` files from a GitHub repo. |

`search_knowledge` and `grep_brain` are intentionally redundant. Vector search is fast and good for "approximately about X"; grep is the only honest answer to "is this exact string in here?" Use them together when an LLM needs to commit to verbatim identifiers.

---

## Internals

```
URLs / GitHub repo
        │
        ▼
 ┌───────────────────────────────────────────┐
 │ Fetcher                                   │
 │   AsyncHTTPCrawlerStrategy   (default)    │  parallel via MemoryAdaptiveDispatcher
 │   AsyncWebCrawler + Chromium (--browser)  │  serial through Playwright
 │   raw.githubusercontent.com  (--github-…) │  urllib direct
 │     ↑ all paths via _check_url_safe       │  scheme + DNS + ipaddress.is_private guard
 └───────────────────────────────────────────┘
        │ HTML, or markdown verbatim
        ▼
 ┌───────────────────────────────────────────┐
 │ DefaultMarkdownGenerator                  │
 │   + PruningContentFilter                  │  threshold 0.48 fixed → fit_markdown
 │     (skipped for --github-docs:           │  source is already markdown)
 │      raw markdown goes through unchanged) │
 └───────────────────────────────────────────┘
        │ clean markdown
        ▼
 ┌───────────────────────────────────────────┐
 │ RecursiveCharacterTextSplitter            │  chunk_size=1000, overlap=100
 └───────────────────────────────────────────┘
        │ list[chunk]
        ▼
 ┌───────────────────────────────────────────┐
 │ SentenceTransformerEmbeddingFunction      │  all-MiniLM-L6-v2, 384-dim, CPU
 │   batched per BFS level                   │  one upsert per level, not per page
 └───────────────────────────────────────────┘
        │ list[(id=sha1(url#i), text, embedding, {source, chunk})]
        ▼
 ┌───────────────────────────────────────────┐
 │ ChromaDB PersistentClient                 │  ./chroma_db/ (SQLite + parquet blobs)
 │   one collection per brain                │  knowledge, terraform-exoscale, …
 │   functools.lru_cache singleton           │  one open SQLite per process
 └───────────────────────────────────────────┘
        ▲
        │ similarity_search / .get / .query
        │
 ┌───────────────────────────────────────────┐
 │ FastMCP server (server.py)                │  stdio transport, six tools
 └───────────────────────────────────────────┘
        ▲
        │
   Claude Code (or any MCP client)
```

### BFS specifics

The web-crawl path uses our own level-by-level BFS instead of `BFSDeepCrawlStrategy` so we can push each level through `arun_many` + `MemoryAdaptiveDispatcher(max_session_permit=workers)`. Per level:

1. Filter the frontier: drop URLs in `visited`, drop URLs matching any `--skip` fnmatch pattern.
2. At the **leaf** level (`level == depth - 1`) also drop URLs already in the brain's `known_sources` (no fetch, no embed).
3. At **intermediate** levels keep known URLs in the wave anyway — we re-fetch them solely for link discovery, but `_clean_markdown` + `_prepare_chunks` short-circuit when `force is False and norm in known`, so embedding never runs on already-stored content.
4. `arun_many` parallel-fetches the wave.
5. For each result, accumulate `_prepare_chunks` output into a level-wide `pending` list.
6. After all results, `_bulk_upsert` runs the entire `pending` list through the embedder in 256-chunk batches with progress prints. One torch graph setup per level instead of per page.
7. Internal links (same-host, http(s) only) get appended to `next_frontier` after dedup against `visited` and `seen_in_next`.

### Chroma client lifetime

`get_collection(brain)` is `@functools.lru_cache(maxsize=None)` on the brain name. `_client()` and `_embed_fn()` are `lru_cache(maxsize=1)`. One `PersistentClient` (one open SQLite handle) and one embedding model (one ~80 MB tensor in RAM) per process, regardless of how many MCP queries arrive. First call: ~3 s to load the model. Every subsequent call: cache hit, microseconds.

### Manifest semantics

Each ingest attempt writes one JSON line to `crawl_log.<brain>.jsonl`:
```json
{"url":"…","status":"stored","chunks":7,"ts":"2026-05-09T09:00:55+00:00"}
{"url":"…","status":"failed","error":"HTTP 404 at …","ts":"…"}
{"url":"…","status":"empty","reason":"no content after pruning filter","ts":"…"}
```
The manifest is append-only. `--status` and `--retry-failed` reduce by latest-per-URL: `_latest_per_url` keeps only the most recent entry for each URL, so a URL that was `failed` then later `stored` shows as `stored`. `--retry-failed` collects URLs whose latest is `failed` or `empty` and re-runs them with `--depth 1 --force`.

### Skip-known correctness across runs

The BFS reachability gotcha: if a URL is skipped at level N-1, the BFS never sees its outgoing links, so children only reachable through it never get discovered. We accept this trade-off — skip-known prevents redundant network *and* embedding cost on resume, at the price of not re-exploring known subtrees. To force full re-exploration use `--force`.

### `coll.get()` paging

ChromaDB's unfiltered `get()` builds a SQL query that hits SQLite's bind-variable cap (~999) once a collection grows past ~10k records. `known_sources()` and `grep_brain()` page through with explicit `limit=1000` + `offset`, breaking when fewer than `page_size` rows come back.

### URL safety boundary

`_check_url_safe(url)` runs at every ingest entrypoint:
- Reject non-http(s) schemes (catches `file://`, `gopher://`, etc.).
- `socket.getaddrinfo` the host, then `ipaddress.ip_address(addr)` each result.
- Reject if any address is `is_private`, `is_loopback`, `is_link_local`, or `is_reserved` (catches RFC1918, `127.0.0.0/8`, `169.254.0.0/16`, IPv6 equivalents).
- `LOCALBRAIN_ALLOW_PRIVATE=1` env var bypasses for legitimate internal-docs use cases.

### Embedding model coupling

Embeddings encode which model produced them — vectors from one model can't be queried with another. `EMBED_MODEL` is fixed at module level. Mixing models within a single brain is impossible because Chroma applies the configured embedding function uniformly. Different brains can in principle use different models, but you'd have to fork `EMBED_MODEL` per brain. Out of scope today; for English docs `all-MiniLM-L6-v2` is a sensible default (384 dims, ~80 MB, fast).

---

## Limitations / non-goals

- **CPU-only embedding by default.** torch wheel is the CPU build; ~100 chunks/s on a modern laptop. Sufficient for ~tens-of-thousands-of-chunks corpora. For larger workloads, install a CUDA/ROCm torch and `sentence-transformers` will pick it up automatically — not worth the setup tax for typical use.
- **No cross-encoder re-ranker.** Top-k from cosine similarity goes straight to the MCP client. Adding a re-ranker would help long-tail queries; not implemented.
- **No auth / multi-user.** Single developer, single machine. Chroma supports server mode if you want shared access; out of scope here.
- **English-tuned embeddings.** `all-MiniLM-L6-v2` was trained mostly on English. For non-English docs, swap in `paraphrase-multilingual-MiniLM-L12-v2` and re-ingest.
- **No automatic re-crawl on doc updates.** Manual: re-run the same `python ingest.py URL ...`; skip-known short-circuits the unchanged pages, `--force` overrides for pages you know changed.
- **BFS link discovery depends on `<a href>` in static HTML.** SPAs without server-rendered links require `--browser` or `--github-docs`.

---

## Repo layout

```
claude-brain/
├── ingest.py                      # CLI + ingest pipeline + helpers
├── server.py                      # FastMCP server (thin wrappers around ingest.py)
├── bootstrap.sh                   # one-shot uv-based setup
├── README.md
├── LICENSE                        # MIT
├── .gitignore                     # ignores chroma_db/, manifests, .venv, settings.local.json
├── chroma_db/                     # generated: vector store (one dir, many collections)
└── crawl_log.<brain>.jsonl        # generated: append-only manifest per brain
```

## License

[MIT](./LICENSE).
