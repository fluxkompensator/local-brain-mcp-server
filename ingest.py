##############################################################
# Created: 2026-05-08
# Author:  void
# Purpose: Crawl one URL or a whole site via parallel BFS, chunk
#          the rendered markdown for each page, embed each chunk
#          with a local sentence-transformer model, and persist
#          everything into a Chroma collection so the LocalBrain
#          MCP server can retrieve it later.
#
#          Default fetcher is pure HTTP (fast, no JS). Use
#          --browser to fall back to Chromium for sites that need
#          JS rendering. URLs already stored are skipped unless
#          --force is passed.
##############################################################
import argparse
import asyncio
import fnmatch
import functools
import gzip
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import chromadb
from chromadb.utils import embedding_functions
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from crawl4ai.async_crawler_strategy import (
    AsyncHTTPCrawlerStrategy,
    HTTPCrawlerConfig,
)
from crawl4ai.async_dispatcher import MemoryAdaptiveDispatcher
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from langchain_text_splitters import RecursiveCharacterTextSplitter

DB_PATH = str(Path(__file__).parent / "chroma_db")
BRAIN_DEFAULT = "knowledge"


EMBED_MODEL = "all-MiniLM-L6-v2"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 LocalBrain/1.0"
)


@functools.lru_cache(maxsize=1)
def _client():
    return chromadb.PersistentClient(path=DB_PATH)


@functools.lru_cache(maxsize=1)
def _embed_fn():
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBED_MODEL
    )


@functools.lru_cache(maxsize=1)
def _reranker():
    """Lazily load the cross-encoder reranker (one-time model download on
    first use, then cached on disk and in-process)."""
    from sentence_transformers import CrossEncoder

    return CrossEncoder(RERANK_MODEL)


@functools.lru_cache(maxsize=None)
def get_collection(brain: str = BRAIN_DEFAULT):
    return _client().get_or_create_collection(
        name=brain, embedding_function=_embed_fn()
    )


def list_brains() -> list[tuple[str, int]]:
    out = []
    for c in _client().list_collections():
        try:
            out.append((c.name, c.count()))
        except Exception:
            out.append((c.name, -1))
    return sorted(out)


def known_sources(coll, page_size: int = 1000) -> set[str]:
    """Return the set of URLs already represented in the collection.

    Pages through results because Chroma's unfiltered get() hits SQLite's
    bind-variable cap on large collections.
    """
    out: set[str] = set()
    offset = 0
    while True:
        page = coll.get(include=["metadatas"], limit=page_size, offset=offset)
        metas = page.get("metadatas") or []
        if not metas:
            break
        for m in metas:
            if src := (m or {}).get("source"):
                out.add(src)
        if len(metas) < page_size:
            break
        offset += page_size
    return out


def grep_brain(
    needle: str,
    brain: str = BRAIN_DEFAULT,
    regex: bool = False,
    limit: int = 20,
    window: int = 120,
    case_sensitive: bool = False,
    page_size: int = 1000,
) -> list[tuple[str, str]]:
    """Substring (or regex) search across every chunk in `brain`.

    Returns up to `limit` (source_url, excerpt) tuples. Excerpt is windowed
    `window` chars on each side of the first match. Set regex=True to treat
    `needle` as a Python regex pattern.

    This complements semantic `search_knowledge` for cases where you need
    to confirm an exact identifier exists (function name, operation ID,
    URL fragment, etc.) — vector search returns near-matches even when the
    literal string is absent.
    """
    coll = get_collection(brain)
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        try:
            pat = re.compile(needle, flags)
        except re.error as e:
            raise ValueError(f"bad regex {needle!r}: {e}") from e
    else:
        pat = re.compile(re.escape(needle), flags)

    out: list[tuple[str, str]] = []
    offset = 0
    while True:
        page = coll.get(
            include=["documents", "metadatas"], limit=page_size, offset=offset
        )
        docs = page.get("documents") or []
        metas = page.get("metadatas") or []
        if not docs:
            break
        for doc, meta in zip(docs, metas):
            m = pat.search(doc or "")
            if not m:
                continue
            start = max(0, m.start() - window)
            end = min(len(doc), m.end() + window)
            snippet = doc[start:end].replace("\n", " ")
            if start > 0:
                snippet = "…" + snippet
            if end < len(doc):
                snippet = snippet + "…"
            src = (meta or {}).get("source", "?")
            out.append((src, snippet))
            if len(out) >= limit:
                return out
        if len(docs) < page_size:
            break
        offset += page_size
    return out


try:
    from rank_bm25 import BM25Okapi

    _HAS_BM25 = True
except ImportError:  # keyword leg is optional; degrade to vector-only
    _HAS_BM25 = False

_TOKEN_RE = re.compile(r"\w+")

# Per-brain BM25 index, cached in-process so back-to-back searches don't
# re-tokenize the whole corpus. Keyed by brain → (count, ids, docs, metas, bm25);
# rebuilt whenever the collection's chunk count changes.
_BM25_CACHE: dict[str, tuple] = {}


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _load_corpus(coll, page_size: int = 1000) -> tuple[list[str], list[str], list[dict]]:
    """Page the whole collection out as (ids, docs, metas), dodging SQLite's
    bind-variable cap the same way known_sources/grep_brain do."""
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    offset = 0
    while True:
        page = coll.get(include=["documents", "metadatas"], limit=page_size, offset=offset)
        pids = page.get("ids") or []
        if not pids:
            break
        ids.extend(pids)
        docs.extend(page.get("documents") or [])
        metas.extend(page.get("metadatas") or [])
        if len(pids) < page_size:
            break
        offset += page_size
    return ids, docs, metas


def _get_bm25(coll, brain: str):
    """Return (ids, docs, metas, bm25) for `brain`, rebuilding the BM25 index
    only when the chunk count has changed. bm25 is None if rank-bm25 isn't
    installed or the brain is empty."""
    count = coll.count()
    cached = _BM25_CACHE.get(brain)
    if cached and cached[0] == count:
        return cached[1:]
    ids, docs, metas = _load_corpus(coll)
    bm25 = BM25Okapi([_tokenize(d) for d in docs]) if (ids and _HAS_BM25) else None
    _BM25_CACHE[brain] = (count, ids, docs, metas, bm25)
    return ids, docs, metas, bm25


def hybrid_search(
    query: str,
    brain: str = BRAIN_DEFAULT,
    k: int = 5,
    candidates: int = 0,
    rrf_k: int = 60,
    rerank: bool = True,
    rerank_pool: int = 0,
) -> list[dict]:
    """Hybrid retrieval: fuse dense vector similarity with BM25 keyword
    ranking via Reciprocal Rank Fusion, then optionally rerank with a
    cross-encoder.

    Vector search returns semantically-near chunks even when the exact term
    is absent; BM25 nails exact identifiers (operation IDs, CLI verbs) but
    misses paraphrase. RRF blends the two ranked lists so a chunk strong on
    either signal surfaces, and one strong on both ranks highest — without
    having to reconcile the two scores' different scales. RRF is a recall
    move: it gets the right chunks into the pool but only knows ranks, not
    relevance. The cross-encoder then reads each (query, chunk) pair jointly
    and reorders the pool by true relevance — the precision move.

    Each leg pulls a candidate pool of `candidates` (default max(k*4, 20))
    and fuses by chunk id with score = Σ 1/(rrf_k + rank). When `rerank` is
    on, the fused top `rerank_pool` (default max(k*3, 15)) are scored by the
    cross-encoder and the top k by that score are returned; otherwise the
    fused top k are returned directly.

    Returns up to k dicts: {id, document, source, score, vrank, brank,
    rerank_score}. vrank/brank are 1-based ranks within each leg (None when
    that leg didn't surface the chunk); rerank_score is the cross-encoder
    logit (None when reranking was off or unavailable). Degrades gracefully
    to RRF order if rank-bm25 or the reranker model is missing.
    """
    coll = get_collection(brain)
    ids_all, docs_all, metas_all, bm25 = _get_bm25(coll, brain)
    if not ids_all:
        return []
    pool = candidates if candidates > 0 else max(k * 4, 20)
    pool = min(pool, len(ids_all))

    vres = coll.query(query_texts=[query], n_results=pool)
    v_ids = (vres.get("ids") or [[]])[0]

    b_ids: list[str] = []
    if bm25 is not None:
        scores = bm25.get_scores(_tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        b_ids = [ids_all[i] for i in ranked[:pool] if scores[i] > 0]

    fused: dict[str, float] = {}
    vrank: dict[str, int] = {}
    brank: dict[str, int] = {}
    for rank, cid in enumerate(v_ids, 1):
        vrank[cid] = rank
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (rrf_k + rank)
    for rank, cid in enumerate(b_ids, 1):
        brank[cid] = rank
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (rrf_k + rank)

    idx = {cid: i for i, cid in enumerate(ids_all)}
    ranked_ids = sorted(fused, key=lambda c: fused[c], reverse=True)

    # Cross-encoder rerank: read the fused top pool jointly with the query
    # and reorder by true relevance. Falls back to RRF order if the model
    # can't load (offline first run, missing dep, etc.).
    rerank_score: dict[str, float] = {}
    if rerank and ranked_ids:
        pool_n = rerank_pool if rerank_pool > 0 else max(k * 3, 15)
        cand = ranked_ids[:pool_n]
        try:
            pairs = [(query, docs_all[idx[c]]) for c in cand]
            scores = _reranker().predict(pairs)
            rerank_score = {c: float(s) for c, s in zip(cand, scores)}
            ranked_ids = sorted(cand, key=lambda c: rerank_score[c], reverse=True)
        except Exception as e:  # never let reranking break retrieval
            print(f"  (reranker unavailable, using RRF order: {e})", file=sys.stderr)

    out: list[dict] = []
    for cid in ranked_ids[:k]:
        i = idx.get(cid)
        if i is None:
            continue
        rs = rerank_score.get(cid)
        out.append(
            {
                "id": cid,
                "document": docs_all[i],
                "source": (metas_all[i] or {}).get("source", "?"),
                "score": round(fused[cid], 6),
                "vrank": vrank.get(cid),
                "brank": brank.get(cid),
                "rerank_score": round(rs, 4) if rs is not None else None,
            }
        )
    return out


def _chunk_id(url: str, idx: int) -> str:
    return hashlib.sha1(f"{url}#{idx}".encode()).hexdigest()


def _clean_markdown(result) -> str:
    """Pull the chrome-stripped markdown from a CrawlResult.

    With our markdown generator wired up, result.markdown is a
    MarkdownGenerationResult with both raw_markdown and fit_markdown.
    We prefer fit_markdown (nav/footer/sidebar pruned) but fall back
    to raw_markdown if the pruner emptied the page.
    """
    md = result.markdown
    if hasattr(md, "fit_markdown"):
        fit = (md.fit_markdown or "").strip()
        if fit:
            return fit
        return (md.raw_markdown or "").strip()
    return (md or "").strip() if isinstance(md, str) else ""


def _prepare_chunks(url: str, markdown: str, splitter) -> list[tuple[str, str, dict]]:
    """Split markdown into chunks and return (id, doc, metadata) tuples.

    No DB I/O here — the level loop accumulates these across all pages
    in a wave so we can do one big embedding call instead of N small ones.
    """
    chunks = splitter.split_text(markdown or "")
    return [
        (_chunk_id(url, i), c, {"source": url, "chunk": i})
        for i, c in enumerate(chunks)
    ]


def _purge_url(coll, url: str) -> None:
    """Drop every chunk currently stored for `url`.

    Needed before re-storing a page whose new chunk count is smaller than
    the old one — otherwise stale orphan chunks remain under their old IDs.
    """
    coll.delete(where={"source": url})


def _bulk_upsert(coll, items: list[tuple[str, str, dict]], batch: int = 256) -> int:
    """Upsert items in fixed-size batches; print progress per batch."""
    if not items:
        return 0
    total = len(items)
    sent = 0
    while sent < total:
        slice_ = items[sent : sent + batch]
        ids, docs, metas = zip(*slice_)
        coll.upsert(ids=list(ids), documents=list(docs), metadatas=list(metas))
        sent += len(slice_)
        print(f"      embedded {sent}/{total} chunks", flush=True)
    return total


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def _normalize(url: str) -> str:
    """Strip URL fragment so /page#anchor1 and /page#anchor2 dedupe."""
    p = urlparse(url)
    return p._replace(fragment="").geturl()


def _check_url_safe(url: str) -> None:
    """Reject schemes other than http/https and hosts that resolve to private,
    loopback, or link-local IPs.

    Boundary check at the MCP / CLI surface: a prompt-injected page or a typo
    shouldn't be able to make us fetch http://169.254.169.254/ (cloud metadata)
    or http://127.0.0.1/. Bypass with LOCALBRAIN_ALLOW_PRIVATE=1 if you really
    need to crawl an internal docs server.
    """
    if os.environ.get("LOCALBRAIN_ALLOW_PRIVATE") == "1":
        return
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise ValueError(f"refusing non-http(s) URL: {url}")
    host = _host(url)
    if not host:
        raise ValueError(f"refusing URL with no host: {url}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS lookup failed for {host}: {e}") from e
    for fam, _, _, _, addr in infos:
        ip = ipaddress.ip_address(addr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(
                f"refusing URL pointing at non-public address {ip} ({host}). "
                f"Set LOCALBRAIN_ALLOW_PRIVATE=1 to override."
            )


def _matches_any(url: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(url, pat) for pat in patterns)


def _log_path(brain: str) -> Path:
    return Path(__file__).parent / f"crawl_log.{brain}.jsonl"


def _log(entry: dict, brain: str) -> None:
    """Append one event line to the crawl manifest for `brain`."""
    entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _log_path(brain).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_log(brain: str) -> list[dict]:
    path = _log_path(brain)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if line.strip():
                out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _latest_per_url(entries: list[dict]) -> dict[str, dict]:
    return {e["url"]: e for e in entries if "url" in e}


def print_status(brain: str) -> None:
    path = _log_path(brain)
    entries = _read_log(brain)
    if not entries:
        print(f"No crawl log yet at {path} (brain={brain}).")
        return
    latest = _latest_per_url(entries)
    cum = Counter(e.get("status", "?") for e in entries)
    cur = Counter(e.get("status", "?") for e in latest.values())
    print(f"Crawl log: {path}  (brain={brain})")
    print(f"  total events:     {len(entries)}")
    print(f"  unique URLs:      {len(latest)}")
    print(f"  cumulative status: {dict(cum)}")
    print(f"  latest status:     {dict(cur)}")
    failed = [u for u, e in latest.items() if e.get("status") in ("failed", "empty")]
    if failed:
        print(f"\n{len(failed)} URLs currently failed/empty (latest entry). First 10:")
        for u in failed[:10]:
            e = latest[u]
            why = e.get("error") or e.get("reason") or e.get("status")
            print(f"  - {u}\n      {why}")
        if len(failed) > 10:
            print(f"  …and {len(failed) - 10} more")


def failed_urls(brain: str) -> list[str]:
    """URLs whose most recent log entry is 'failed' or 'empty' for `brain`."""
    latest = _latest_per_url(_read_log(brain))
    return [u for u, e in latest.items() if e.get("status") in ("failed", "empty")]


_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _github_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _github_default_branch(repo: str) -> str:
    return _github_get_json(f"https://api.github.com/repos/{repo}").get(
        "default_branch", "main"
    )


def _github_md_files(repo: str, ref: str, prefix: str) -> list[str]:
    """List paths of all .md files under `prefix` in the repo's tree at `ref`."""
    data = _github_get_json(
        f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
    )
    if data.get("truncated"):
        print(
            "  ! GitHub returned a truncated tree (>100k items). "
            "Some files may be missing.",
            file=sys.stderr,
        )
    out: list[str] = []
    for item in data.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "") or ""
        if (not prefix or path.startswith(prefix)) and path.lower().endswith(".md"):
            out.append(path)
    return sorted(out)


def _github_fetch_raw(repo: str, ref: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def ingest_github_docs(
    repo: str,
    brain: str = BRAIN_DEFAULT,
    path_prefix: str = "docs/",
    ref: str | None = None,
    force: bool = False,
) -> int:
    """Ingest every .md file under `path_prefix` from a GitHub repo into `brain`.

    GitHub raw markdown is the source format — no HTML→markdown conversion,
    no SPA workarounds. Same chunk/embed/log path as web crawls so the
    manifest, --status, --coverage all keep working.
    """
    if "/" not in repo:
        raise ValueError(f"--github-docs expects OWNER/REPO, got: {repo}")
    if ref is None:
        ref = _github_default_branch(repo)
    print(
        f"→ GitHub: {repo}@{ref} (prefix={path_prefix or '<all>'}, brain={brain}, "
        f"force={force})"
    )
    files = _github_md_files(repo, ref, path_prefix)
    if not files:
        print("  no markdown files found")
        return 0
    print(f"  found {len(files)} .md files")

    coll = get_collection(brain)
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    known = set() if force else known_sources(coll)

    pending: list[tuple[str, str, dict]] = []
    new_pages = 0
    for path in files:
        url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
        norm = _normalize(url)
        if not force and norm in known:
            print(f"  ↷ {path} already stored")
            continue
        try:
            md = _github_fetch_raw(repo, ref, path)
        except Exception as e:
            err = str(e)[:300]
            print(f"  ✗ {path}: {err}", file=sys.stderr)
            _log({"url": norm, "status": "failed", "error": err}, brain)
            continue
        chunks = _prepare_chunks(norm, md, splitter)
        if not chunks:
            _log(
                {"url": norm, "status": "empty", "reason": "empty markdown"},
                brain,
            )
            print(f"  · {path} (empty)")
            continue
        if force:
            _purge_url(coll, norm)
        pending.extend(chunks)
        new_pages += 1
        _log(
            {"url": norm, "status": "stored", "chunks": len(chunks)},
            brain,
        )
        print(f"  ✓ {path} ({len(chunks)} chunks)")

    if pending:
        print(
            f"\nembedding {len(pending)} chunks from {new_pages} new files "
            f"(CPU, all-MiniLM-L6-v2)…",
            flush=True,
        )
        _bulk_upsert(coll, pending)
    print(f"✓ done — {new_pages} files stored from {repo}@{ref}")
    return new_pages


def _fetch_sitemap_bytes(url: str) -> bytes:
    _check_url_safe(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _sitemap_urls(url: str, seen: set[str] | None = None) -> set[str]:
    """Recursively follow sitemap indexes and collect all <loc> URLs."""
    seen = seen if seen is not None else set()
    if url in seen:
        return set()
    seen.add(url)
    print(f"  fetching {url}")
    try:
        data = _fetch_sitemap_bytes(url)
    except Exception as e:
        print(f"    ✗ {e}", file=sys.stderr)
        return set()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"    ✗ parse error: {e}", file=sys.stderr)
        return set()

    out: set[str] = set()
    if root.tag.endswith("sitemapindex"):
        for sm in root.findall(f"{_SITEMAP_NS}sitemap"):
            loc = sm.find(f"{_SITEMAP_NS}loc")
            if loc is not None and loc.text:
                out |= _sitemap_urls(loc.text.strip(), seen)
        return out
    for u in root.findall(f"{_SITEMAP_NS}url"):
        loc = u.find(f"{_SITEMAP_NS}loc")
        if loc is not None and loc.text:
            out.add(_normalize(loc.text.strip()))
    return out


def _resolve_sitemap_url(url: str) -> str:
    """Accept either a sitemap URL or a site root; return the sitemap URL."""
    if url.endswith(".xml") or url.endswith(".xml.gz") or "/sitemap" in url:
        return url
    return url.rstrip("/") + "/sitemap.xml"


def coverage_report(url: str, brain: str) -> tuple[set[str], set[str]]:
    """Print a coverage report. Returns (in_db, missing) for further use."""
    sm_url = _resolve_sitemap_url(url)
    print(f"Coverage report against {sm_url}  (brain={brain})")
    sitemap = _sitemap_urls(sm_url)
    if not sitemap:
        print("  (no URLs found in sitemap)")
        return set(), set()
    db = known_sources(get_collection(brain))
    failed = set(failed_urls(brain))
    in_db = sitemap & db
    in_failed = sitemap & failed
    missing = sitemap - db - failed
    pct = 100 * len(in_db) / len(sitemap)
    print(f"  sitemap URLs:                {len(sitemap)}")
    print(f"  ✓ stored in DB:              {len(in_db)}  ({pct:.1f}%)")
    print(f"  ✗ failed/empty in manifest:  {len(in_failed)}")
    print(f"  ? missing (never attempted): {len(missing)}")
    extra = db - sitemap
    if extra:
        print(f"  (db has {len(extra)} URL(s) not in sitemap — usually fine)")
    if missing:
        print(f"\nFirst 20 missing URLs:")
        for u in sorted(missing)[:20]:
            print(f"  - {u}")
        if len(missing) > 20:
            print(f"  …and {len(missing) - 20} more")
    return in_db, missing


def _build_crawler(browser: bool, workers: int) -> AsyncWebCrawler:
    if browser:
        return AsyncWebCrawler(
            config=BrowserConfig(
                browser_type="chromium", headless=True, user_agent=USER_AGENT
            )
        )
    http_cfg = HTTPCrawlerConfig(
        follow_redirects=True,
        verify_ssl=True,
        headers={"User-Agent": USER_AGENT},
    )
    strategy = AsyncHTTPCrawlerStrategy(
        browser_config=http_cfg,
        max_connections=max(workers * 2, 32),
    )
    return AsyncWebCrawler(crawler_strategy=strategy)


def _internal_links(result) -> list[str]:
    """Pull HTTP(S) link hrefs from a CrawlResult's 'internal' bucket."""
    links = result.links or {}
    items = links.get("internal", []) if isinstance(links, dict) else []
    out = []
    for item in items:
        href = item.get("href") if isinstance(item, dict) else item
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            out.append(_normalize(href))
    return out


async def ingest(
    url: str,
    depth: int = 1,
    max_pages: int = 50,
    force: bool = False,
    workers: int = 16,
    browser: bool = False,
    skip: list[str] | None = None,
    brain: str = BRAIN_DEFAULT,
) -> int:
    """Ingest one URL (depth=1) or BFS-crawl rooted at it (depth>=2) into `brain`.

    depth>=2 explores same-host links, depth-1 hops out from the root.
    Already-stored URLs are skipped unless force=True. URLs matching any
    fnmatch pattern in `skip` are filtered out of the BFS frontier.
    """
    skip = skip or []
    _check_url_safe(url)
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    coll = get_collection(brain)
    known = set() if force else known_sources(coll)

    root = _normalize(url)
    root_host = _host(root)
    visited: set[str] = set()
    frontier: list[str] = [root]
    total_chunks = 0
    pages_done = 0

    md_generator = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(threshold=0.48, threshold_type="fixed")
    )
    run_cfg = CrawlerRunConfig(stream=False, markdown_generator=md_generator)
    dispatcher = MemoryAdaptiveDispatcher(max_session_permit=workers)

    print(
        f"→ {'BFS' if depth > 1 else 'fetch'} rooted at {root} "
        f"(brain={brain}, depth={depth}, max_pages={max_pages}, "
        f"workers={workers}, mode={'browser' if browser else 'http'}, "
        f"already-known={len(known)}, skip-patterns={len(skip)})"
    )

    async with _build_crawler(browser, workers) as crawler:
        for level in range(depth):
            is_leaf = level + 1 >= depth
            # At the leaf level skip known URLs entirely (nothing to discover).
            # At intermediate levels still fetch them — we need their link
            # graph to discover new pages — but we won't re-store them.
            if is_leaf:
                wave = [
                    u for u in frontier
                    if u not in visited
                    and not _matches_any(u, skip)
                    and (force or u not in known)
                ]
            else:
                wave = [
                    u for u in frontier
                    if u not in visited and not _matches_any(u, skip)
                ]
            if not wave:
                break
            if pages_done + len(wave) > max_pages:
                wave = wave[: max_pages - pages_done]
            if not wave:
                break

            print(f"  level {level}: fetching {len(wave)} pages in parallel", flush=True)
            results = await crawler.arun_many(
                urls=wave, config=run_cfg, dispatcher=dispatcher
            )

            next_frontier: list[str] = []
            seen_in_next: set[str] = set()
            pending: list[tuple[str, str, dict]] = []
            level_new_pages = 0
            for r in results:
                norm = _normalize(r.url)
                visited.add(norm)
                pages_done += 1
                if not r.success:
                    err = r.error_message or "unknown error"
                    # Trim verbose code-context dumps for the manifest.
                    short_err = err.splitlines()[0][:300] if err else "unknown"
                    print(f"    ✗ {r.url}: {short_err}", file=sys.stderr)
                    _log({"url": norm, "status": "failed", "error": short_err}, brain)
                    continue
                if force or norm not in known:
                    page_chunks = _prepare_chunks(norm, _clean_markdown(r), splitter)
                    if page_chunks:
                        if force:
                            _purge_url(coll, norm)
                        level_new_pages += 1
                        pending.extend(page_chunks)
                        _log(
                            {
                                "url": norm,
                                "status": "stored",
                                "chunks": len(page_chunks),
                            },
                            brain,
                        )
                    else:
                        _log(
                            {
                                "url": norm,
                                "status": "empty",
                                "reason": "no content after pruning filter",
                            },
                            brain,
                        )
                if is_leaf:
                    continue
                for link in _internal_links(r):
                    if _host(link) != root_host:
                        continue
                    if _matches_any(link, skip):
                        continue
                    if link in visited or link in seen_in_next:
                        continue
                    seen_in_next.add(link)
                    next_frontier.append(link)

            if pending:
                print(
                    f"    embedding {len(pending)} chunks from "
                    f"{level_new_pages} new pages (CPU, all-MiniLM-L6-v2)…",
                    flush=True,
                )
            level_chunks = _bulk_upsert(coll, pending)
            total_chunks += level_chunks

            print(
                f"    level {level} done: fetched {len(wave)}, "
                f"stored {level_new_pages} new pages ({level_chunks} chunks); "
                f"queued {len(next_frontier)} for next level"
            )
            frontier = next_frontier
            if pages_done >= max_pages:
                print(f"    (max_pages={max_pages} reached)")
                break

    print(
        f"✓ done — {pages_done} pages processed, "
        f"{total_chunks} chunks stored from crawl rooted at {root}"
    )
    return total_chunks


async def _run(
    urls: list[str],
    depth: int,
    max_pages: int,
    force: bool,
    workers: int,
    browser: bool,
    skip: list[str],
    brain: str,
) -> None:
    for u in urls:
        await ingest(
            u,
            depth=depth,
            max_pages=max_pages,
            force=force,
            workers=workers,
            browser=browser,
            skip=skip,
            brain=brain,
        )


def reset_db(brain: str) -> None:
    """Drop just the named brain's collection. Other brains are untouched."""
    client = chromadb.PersistentClient(path=DB_PATH)
    try:
        client.delete_collection(name=brain)
        print(f"✓ deleted brain '{brain}'")
    except Exception as e:
        print(f"(brain '{brain}' not present or already gone: {e})")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Add URLs (or whole sites via parallel BFS) to the LocalBrain knowledge base."
    )
    p.add_argument("urls", nargs="*", help="URL(s) to fetch — root URL(s) when --depth>1.")
    p.add_argument(
        "--reset",
        action="store_true",
        help="Wipe the entire collection before crawling (or by itself).",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=1,
        help="1 = single page (default). 2+ = BFS crawl following links up to depth-1 hops.",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Cap total pages per BFS root (default 50). Ignored when --depth=1.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Concurrent fetches per BFS level (default 16).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and overwrite URLs even if they are already in the DB.",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help="Use Chromium for fetching (slower but renders JS). Default: pure HTTP.",
    )
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="PATTERN",
        help="fnmatch URL pattern to drop from BFS (repeatable). Examples: "
             "'*.yaml', '*/sla/*', '*/_schemas/*'.",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Print crawl-log summary and exit.",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-crawl every URL whose latest log entry is failed/empty (depth=1, force=True).",
    )
    p.add_argument(
        "--coverage",
        metavar="URL",
        help="Compare DB to a site's sitemap.xml. Pass the sitemap URL or just the site root.",
    )
    p.add_argument(
        "--ingest-missing",
        metavar="URL",
        help="Fetch sitemap.xml, then ingest URLs in the sitemap that aren't in the DB (depth=1).",
    )
    p.add_argument(
        "--brain",
        default=BRAIN_DEFAULT,
        metavar="NAME",
        help=f"Which brain (Chroma collection) to read/write. Default: {BRAIN_DEFAULT}.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List all brains in the DB with their chunk counts and exit.",
    )
    p.add_argument(
        "--github-docs",
        metavar="OWNER/REPO[@REF]",
        help="Ingest all .md files under docs/ from a GitHub repo. Skips the SPA mess.",
    )
    p.add_argument(
        "--github-docs-path",
        default="docs/",
        metavar="PREFIX",
        help="Path prefix in the repo (default: docs/). Pass '' for all .md files.",
    )
    p.add_argument(
        "--grep",
        metavar="NEEDLE",
        help="Literal substring search across every chunk in --brain. Pairs with --regex.",
    )
    p.add_argument(
        "--regex",
        action="store_true",
        help="Treat --grep NEEDLE as a regex (Python re syntax).",
    )
    p.add_argument(
        "--grep-limit",
        type=int,
        default=20,
        metavar="N",
        help="Max --grep matches to print (default 20).",
    )
    args = p.parse_args()

    if args.list:
        brains = list_brains()
        if not brains:
            print("No brains yet. Crawl something first.")
        else:
            print(f"{'BRAIN':<32}  CHUNKS")
            for name, n in brains:
                print(f"{name:<32}  {n}")
        return
    if args.status:
        print_status(args.brain)
        return
    if args.grep:
        try:
            hits = grep_brain(
                args.grep,
                brain=args.brain,
                regex=args.regex,
                limit=args.grep_limit,
            )
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(2)
        if not hits:
            print(f"No matches for {args.grep!r} in brain '{args.brain}'.")
            return
        print(f"{len(hits)} match(es) for {args.grep!r} in brain '{args.brain}':\n")
        for src, snippet in hits:
            print(f"[{src}]")
            print(f"  {snippet}\n")
        return
    if args.coverage:
        coverage_report(args.coverage, args.brain)
        return
    if args.reset:
        reset_db(args.brain)
    if args.github_docs:
        repo, _, ref = args.github_docs.partition("@")
        ingest_github_docs(
            repo,
            brain=args.brain,
            path_prefix=args.github_docs_path,
            ref=ref or None,
            force=args.force,
        )
        return
    if args.ingest_missing:
        _, missing = coverage_report(args.ingest_missing, args.brain)
        if not missing:
            print("\nNothing missing — DB matches sitemap.")
            return
        print(f"\nIngesting {len(missing)} missing URLs at depth=1 into '{args.brain}'…")
        asyncio.run(
            _run(
                sorted(missing),
                depth=1,
                max_pages=len(missing) + 1,
                force=False,
                workers=args.workers,
                browser=args.browser,
                skip=args.skip,
                brain=args.brain,
            )
        )
        return
    if args.retry_failed:
        urls = failed_urls(args.brain)
        if not urls:
            print("No failed/empty URLs in the manifest to retry.")
            return
        print(f"Retrying {len(urls)} URLs at depth=1 with force=True (brain={args.brain})…")
        asyncio.run(
            _run(
                urls,
                depth=1,
                max_pages=len(urls) + 1,
                force=True,
                workers=args.workers,
                browser=args.browser,
                skip=args.skip,
                brain=args.brain,
            )
        )
        return
    if not args.urls:
        if not args.reset:
            p.error(
                "urls required (or pass one of --reset / --list / --status / "
                "--retry-failed / --coverage / --ingest-missing)"
            )
        return
    asyncio.run(
        _run(
            args.urls,
            args.depth,
            args.max_pages,
            args.force,
            args.workers,
            args.browser,
            args.skip,
            args.brain,
        )
    )


if __name__ == "__main__":
    main()
