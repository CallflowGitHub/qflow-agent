"""
Agentic RAG tool: hybrid search with FlashRank re-ranking.

Architecture
------------
1. Hybrid retrieval — calls the existing ``search_qflow_kb_hybrid`` Supabase RPC, which
   runs pgvector cosine similarity + PostgreSQL FTS (websearch_to_tsquery) and fuses them
   with Reciprocal Rank Fusion (RRF) entirely inside a single SQL query.
2. Re-ranking       — FlashRank cross-encoder on the RPC results, returning the top-N
   most relevant chunks.

The ``search_qflow_kb_hybrid`` function signature expected in Supabase:
    search_qflow_kb_hybrid(
        query_embedding  vector,
        query_text       text,
        match_count      int,
        rrf_k            float   -- typically 60.0
    )
    RETURNS TABLE (id, source_file, title, category, description, tags,
                   chunk_index, heading_path, chunk_text, rrf_score)

The ``search_qflow_kb_semantic`` function signature expected in Supabase:
    search_qflow_kb_semantic(
        query_embedding  vector,
        match_threshold  float,
        match_count      int
    )
    RETURNS TABLE (id, source_file, title, category, description, tags,
                   chunk_index, heading_path, chunk_text, similarity)
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated, Literal

from agent_framework import tool
from flashrank import Ranker, RerankRequest
from openai import AsyncAzureOpenAI
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Lazy-initialised singletons (initialised on first tool call, after load_dotenv)
# ---------------------------------------------------------------------------

_supabase_client: Client | None = None
_embedding_client: AsyncAzureOpenAI | None = None
_ranker: Ranker | None = None

_RRF_K = 60.0  # Standard constant — same default used in Azure AI Search


def _get_supabase() -> Client:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
    return _supabase_client


def _get_embedding_client() -> AsyncAzureOpenAI:
    global _embedding_client
    if _embedding_client is None:
        _embedding_client = AsyncAzureOpenAI(
            api_key=os.environ["AZURE_AI_KEY"],
            azure_endpoint=os.environ["AZURE_AI_ENDPOINT"],
            azure_deployment=os.environ["EMBEDDING_DEPLOYMENT_NAME"],
            api_version="2025-03-01-preview",
        )
    return _embedding_client


def _get_ranker() -> Ranker:
    global _ranker
    if _ranker is None:
        # ms-marco-MiniLM-L-12-v2 — best quality/speed trade-off in FlashRank
        _ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2")
    return _ranker


# ---------------------------------------------------------------------------
# Retrieval helpers (sync Supabase I/O — called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _hybrid_search(embedding: list[float], query_text: str, match_count: int) -> list[dict]:
    """Call the ``search_qflow_kb_hybrid`` RPC — RRF fusion happens inside SQL."""
    result = _get_supabase().rpc(
        "search_qflow_kb_hybrid",
        {
            "query_embedding": embedding,
            "query_text": query_text,
            "match_count": match_count,
            "rrf_k": _RRF_K,
        },
    ).execute()
    return result.data or []


def _semantic_search(
    embedding: list[float], match_count: int, match_threshold: float
) -> list[dict]:
    """Call the ``search_qflow_kb_semantic`` RPC — pure vector similarity."""
    result = _get_supabase().rpc(
        "search_qflow_kb_semantic",
        {
            "query_embedding": embedding,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return result.data or []


# ---------------------------------------------------------------------------
# Re-ranking: FlashRank cross-encoder
# ---------------------------------------------------------------------------


def _rerank(query: str, candidates: list[dict], top_n: int) -> list[dict]:
    """Re-rank candidates with a FlashRank cross-encoder and return top_n."""
    if not candidates:
        return []

    ranker = _get_ranker()
    passages = [
        {"id": str(doc["id"]), "text": doc.get("chunk_text", "")}
        for doc in candidates
    ]
    rerank_request = RerankRequest(query=query, passages=passages)
    ranked = ranker.rerank(rerank_request)

    id_to_doc = {str(doc["id"]): doc for doc in candidates}
    return [
        id_to_doc[r["id"]]
        for r in ranked[:top_n]
        if r["id"] in id_to_doc
    ]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_results(docs: list[dict], score_key: str) -> str:
    if not docs:
        return "No relevant documents found in the knowledge base."

    parts: list[str] = []
    for i, doc in enumerate(docs, 1):
        heading = f" › {doc['heading_path']}" if doc.get("heading_path") else ""
        category = f" | Category: {doc['category']}" if doc.get("category") else ""
        score = doc.get(score_key)
        score_str = f" | Score: {score:.4f}" if score is not None else ""
        header = (
            f"[{i}] {doc.get('title', 'Untitled')}{heading}\n"
            f"Source: {doc.get('source_file', 'unknown')}{category}{score_str}"
        )
        parts.append(f"{header}\n\n{doc.get('chunk_text', '').strip()}")

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


_MATCH_COUNT = 20  # Candidates fetched from DB before re-ranking
_TOP_N = 5        # Final results returned after re-ranking
_SEMANTIC_THRESHOLD = 0.5  # Minimum cosine similarity for semantic mode


@tool
async def search_knowledge_base(
    query: Annotated[str, "The user's question or topic to look up in the Q-Flow knowledge base"],
    mode: Annotated[
        Literal["hybrid", "semantic"],
        "Search mode. "
        "Use 'hybrid' (default) for most questions — it combines vector similarity and "
        "full-text keyword search, making it robust for specific feature names, error messages, "
        "configuration terms, and natural-language questions alike. "
        "Use 'semantic' only when the query is purely conceptual or exploratory and contains "
        "no product-specific keywords (e.g. 'how does workflow automation generally work').",
    ] = "hybrid",
) -> str:
    """Search the Q-Flow product knowledge base and return the most relevant articles.

    Call this tool for ANY question about Q-Flow: features, configuration, integrations,
    troubleshooting, how-tos, release notes, or best practices. Always call this before
    composing an answer — never rely on general knowledge alone for Q-Flow-specific topics.

    How it works:
    - hybrid mode (default): runs pgvector semantic search + PostgreSQL full-text search in
      parallel inside a single SQL query, fuses results with Reciprocal Rank Fusion (RRF),
      then re-ranks the fused candidates with a FlashRank cross-encoder.
    - semantic mode: runs pgvector cosine similarity only, filtered by a minimum score
      threshold, then re-ranks with the same FlashRank cross-encoder.

    Returns the top ranked knowledge base chunks with title, source file, category,
    heading path, and the chunk text. Synthesise these into your answer and cite the source.
    """
    # Embed the query — needed by both modes
    embedding_response = await _get_embedding_client().embeddings.create(
        input=query,
        model=os.environ["EMBEDDING_DEPLOYMENT_NAME"],
    )
    query_vector: list[float] = embedding_response.data[0].embedding

    if mode == "hybrid":
        candidates = await asyncio.to_thread(
            _hybrid_search, query_vector, query, _MATCH_COUNT
        )
        score_key = "rrf_score"
    else:
        candidates = await asyncio.to_thread(
            _semantic_search, query_vector, _MATCH_COUNT, _SEMANTIC_THRESHOLD
        )
        score_key = "similarity"

    reranked = await asyncio.to_thread(_rerank, query, candidates, _TOP_N)
    return _format_results(reranked, score_key)
