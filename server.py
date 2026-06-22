##############################################################
# Created: 2026-05-08
# Author:  void
# Purpose: MCP server exposing the local Chroma knowledge base
#          to Claude Code over stdio. Provides search_knowledge
#          for retrieval, learn_url / learn_site for ingestion,
#          and list_brains for discovering which brains exist.
#          All tools accept a `brain` parameter so Claude can
#          target a specific topical brain.
##############################################################
from mcp.server.fastmcp import FastMCP

from ingest import (
    BRAIN_DEFAULT,
    grep_brain as _grep_brain,
    hybrid_search,
    ingest,
    ingest_github_docs,
    list_brains as _list_brains,
)

mcp = FastMCP("LocalBrain")


@mcp.tool()
def list_brains() -> str:
    """List every brain (Chroma collection) currently in the local DB and its chunk count.

    Use this first when you don't know which brain holds the topic you want to search.
    """
    brains = _list_brains()
    if not brains:
        return "No brains yet. Use learn_url or learn_site to populate one."
    return "\n".join(f"{name}\t{n} chunks" for name, n in brains)


@mcp.tool()
def grep_brain(
    needle: str,
    brain: str = BRAIN_DEFAULT,
    regex: bool = False,
    limit: int = 20,
) -> str:
    """Literal substring (or regex) search across every chunk in a brain.

    Use this when you need to confirm whether an *exact* identifier
    (function name, API operationId, URL fragment, CLI verb) actually
    exists in the docs. Vector search via search_knowledge returns
    semantically-near chunks even when the literal string isn't present
    — grep_brain tells you for sure whether the string is in the corpus.

    Args:
        needle: Literal substring to find (case-insensitive). With regex=True,
            this is treated as a Python regex pattern.
        brain: Which brain to search.
        regex: True to treat needle as a regex; default False (literal).
        limit: Max matches to return (default 20). Each match returns a
            ~120-char window around the hit.

    Returns lines of "[source_url]\\n  snippet…" pairs, or "No matches" if absent.
    """
    try:
        hits = _grep_brain(needle, brain=brain, regex=regex, limit=limit)
    except ValueError as e:
        return f"Error: {e}"
    if not hits:
        return f"No matches for {needle!r} in brain '{brain}'."
    return "\n\n".join(f"[{src}]\n  {snippet}" for src, snippet in hits)


@mcp.tool()
def search_knowledge(query: str, k: int = 5, brain: str = BRAIN_DEFAULT) -> str:
    """Search a brain for chunks relevant to the query (hybrid + rerank).

    Fuses dense vector similarity with BM25 keyword ranking via Reciprocal
    Rank Fusion, then reorders the pool with a cross-encoder reranker that
    reads each (query, chunk) pair jointly. Semantic matches surface
    paraphrases, BM25 nails exact identifiers (operation IDs, CLI verbs,
    function names), and the reranker sharpens final ordering by true
    relevance. Each result is tagged with how it was surfaced — e.g.
    (vec#2+bm25#1 rr=6.42) means it ranked 2nd by vectors, 1st by keywords,
    with cross-encoder score 6.42. For a pure literal "does this exact
    string exist" check, use grep_brain instead.

    Args:
        query: Natural-language question or topic to look up.
        k: Number of chunks to return (default 5).
        brain: Which brain to search. Default: 'knowledge'. Use list_brains
            to see what's available (e.g. 'exoscale-community',
            'terraform-exoscale').
    """
    hits = hybrid_search(query, brain=brain, k=k)
    if not hits:
        return f"No matches in brain '{brain}'."

    parts = []
    for h in hits:
        tags = []
        if h["vrank"]:
            tags.append(f"vec#{h['vrank']}")
        if h["brank"]:
            tags.append(f"bm25#{h['brank']}")
        signal = "+".join(tags) or "?"
        if h.get("rerank_score") is not None:
            signal += f" rr={h['rerank_score']}"
        parts.append(f"[source: {h['source']}] ({signal})\n{h['document']}")
    return "\n\n---\n\n".join(parts)


@mcp.tool()
async def learn_url(
    url: str,
    force: bool = False,
    browser: bool = False,
    brain: str = BRAIN_DEFAULT,
) -> str:
    """Fetch a URL, chunk it, embed it, and store it in `brain`.

    Skips fetching if the URL is already stored in `brain` unless force=True.
    Default fetcher is pure HTTP (fast). Pass browser=True for JS-rendered sites.
    """
    n = await ingest(url, force=force, browser=browser, brain=brain)
    if n == 0:
        return f"Nothing stored in '{brain}' for {url} (already known, fetch failed, or empty)."
    return f"Stored {n} chunks from {url} in brain '{brain}'."


@mcp.tool()
async def learn_site(
    url: str,
    depth: int = 2,
    max_pages: int = 30,
    workers: int = 16,
    force: bool = False,
    browser: bool = False,
    skip: list[str] | None = None,
    brain: str = BRAIN_DEFAULT,
) -> str:
    """BFS-crawl a site rooted at url and store every page found in `brain`.

    URLs already in `brain` are skipped (no re-fetch) unless force=True.
    Default fetcher is pure HTTP. Pass browser=True for SPA-style sites.
    `skip` is a list of fnmatch URL patterns to drop from the BFS frontier
    (e.g. ["*.yaml", "*/sla/*"]).

    Args:
        url: Root URL to crawl from.
        depth: How many hops to follow (2 = root + first-hop links).
        max_pages: Hard cap on total pages crawled.
        workers: Concurrent fetches per BFS level.
        force: Re-fetch and overwrite even if URLs are already stored.
        browser: Use Chromium instead of pure HTTP (JS-rendered sites).
        skip: fnmatch URL patterns to filter out of the BFS frontier.
        brain: Which brain to store into. Pick a topical name (e.g.
            'terraform-exoscale'); `list_brains` shows existing ones.
    """
    n = await ingest(
        url,
        depth=depth,
        max_pages=max_pages,
        workers=workers,
        force=force,
        browser=browser,
        skip=skip or [],
        brain=brain,
    )
    if n == 0:
        return f"No new chunks stored in '{brain}' for crawl rooted at {url}."
    return f"Stored {n} new chunks from crawl rooted at {url} in brain '{brain}'."


@mcp.tool()
def learn_github_docs(
    repo: str,
    brain: str = BRAIN_DEFAULT,
    path_prefix: str = "docs/",
    ref: str = "",
    force: bool = False,
) -> str:
    """Ingest all markdown files under `path_prefix` from a GitHub repo into `brain`.

    Use this for projects whose docs are JS-rendered on a public site
    (Terraform Registry, MkDocs Material, etc.) but whose source markdown
    lives in a public GitHub repo. Much faster and cleaner than scraping
    the rendered site.

    Args:
        repo: "owner/name", e.g. "exoscale/terraform-provider-exoscale".
        brain: Which brain to store into.
        path_prefix: Path within the repo (default "docs/"). "" matches all .md.
        ref: Branch/tag/SHA. Empty string auto-detects the default branch.
        force: Re-fetch and overwrite existing chunks.
    """
    n = ingest_github_docs(
        repo,
        brain=brain,
        path_prefix=path_prefix,
        ref=ref or None,
        force=force,
    )
    if n == 0:
        return f"Nothing stored in '{brain}' from {repo} (no .md files or all already known)."
    return f"Stored {n} markdown files from {repo} in brain '{brain}'."


if __name__ == "__main__":
    mcp.run()
