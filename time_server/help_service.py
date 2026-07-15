"""
Project-help "brain" — RAG-grounded answers about the project, server-side.

This is the remote counterpart to the CLI's `/help` command. The CLI is a thin
client; the retrieval + generation happen here so the knowledge base lives in one
place (and later on Cloud Run, off any local network).

It deliberately reuses jarvis-cli's primitives rather than duplicating them:
retrieval (`jarvis.indexing`), the grounded prompt block
(`jarvis.prompt_builder.build_rag_block`), the mandatory-citation appendix
(`jarvis.rag.cite`), and the provider-agnostic LLM gateway (`jarvis.llm`). Install
jarvis-cli into this server's environment (see requirements.txt).

Agent tuning (chosen for a grounded, factual, single-shot Q&A agent — not left on
defaults): low temperature, a `direct` answer (RAG does the reasoning), closed-domain
(decline when the docs don't cover it), and a bounded answer length. See the
`JARVIS_HELP_*` env vars below.

Config (env):
    JARVIS_HELP_INDEX        index name to answer from      (default "jarvis-docs")
    JARVIS_HELP_MODEL        generation model               (default: OpenRouter default)
    JARVIS_HELP_K            chunks retrieved per question  (default 5)
    JARVIS_HELP_MIN_SCORE    closed-domain relevance bar    (default 0.15)
    JARVIS_HELP_TEMPERATURE  sampling temperature           (default 0.2)
    JARVIS_HELP_MAX_TOKENS   max answer tokens              (default 800)
    JARVIS_INDEX_DIR         where indexes live             (default ~/.jarvis/indexes)
"""

from __future__ import annotations

import os

# All reuse comes from the installed jarvis-cli package.
from jarvis.indexing import IndexPipeline, IndexStore, make_embedder
from jarvis.prompt_builder.builder import build_rag_block
from jarvis.rag.cite import build_citations, idk_message, strip_trailing_citations
from jarvis.llm.gateway import LLMGateway
from jarvis.llm.router import make_engine
from jarvis.openrouter.client import DEFAULT_MODEL as _CLOUD_DEFAULT


class HelpError(Exception):
    """A caller-facing failure (bad request / unavailable retrieval)."""


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


_SYSTEM = (
    "You are the documentation assistant for THIS project (the jarvis-cli codebase). "
    "Answer questions about the project using ONLY the knowledge-base excerpts "
    "provided in this turn. Be concise and technical. Cite the excerpts you use "
    "inline as [n]. If the excerpts do not cover the question, say so plainly "
    "instead of guessing."
)


def _sources(results: list[dict]) -> list[str]:
    """Unique source filenames, in first-seen order, for the response payload."""
    seen: list[str] = []
    for r in results:
        fn = (r.get("metadata") or {}).get("filename", "?")
        if fn not in seen:
            seen.append(fn)
    return seen


def answer(question: str, branch: str | None = None) -> dict:
    """Answer a project question, grounded in the docs index.

    Returns ``{answer, sources, branch, grounded, notice}``. Raises ``HelpError``
    for bad input or unavailable retrieval, so the route can map it to a clean 4xx
    instead of a 500.
    """
    question = (question or "").strip()
    if not question:
        raise HelpError("question must be a non-empty string")

    index_name = os.environ.get("JARVIS_HELP_INDEX", "jarvis-docs").strip() or "jarvis-docs"
    k = _i("JARVIS_HELP_K", 5)
    min_score = _f("JARVIS_HELP_MIN_SCORE", 0.15)

    # ── Retrieve (embed the query the same way the index was built) ──────────────
    store = IndexStore()
    header = store.load_header(index_name)
    if header is None:
        raise HelpError(
            f"index '{index_name}' not found. Build it (index build docs/ "
            f"name={index_name}) and point JARVIS_INDEX_DIR at it."
        )
    try:
        embedder = make_embedder(header.get("provider"), header.get("model"))
        results = IndexPipeline(embedder, store).search(index_name, question, k)
    except Exception as exc:  # noqa: BLE001 — retrieval unreachable → clean error
        raise HelpError(f"retrieval failed: {exc}") from exc

    # ── Closed-domain gate: decline when nothing is relevant enough ──────────────
    best = max((r.get("score", 0.0) for r in results), default=0.0)
    if not results or best < min_score:
        return {
            "answer": idk_message(question, best, min_score),
            "sources": [],
            "branch": branch,
            "grounded": False,
            "notice": f"no excerpt cleared the relevance bar (best {best:.2f} < {min_score:.2f})",
        }

    # ── Build the grounded prompt (reuse the same block the CLI RAG chat uses) ────
    messages: list[dict] = [{"role": "system", "content": _SYSTEM}]
    if branch:
        messages.append({
            "role": "system",
            "content": f"Context: the developer is currently on git branch '{branch}'.",
        })
    messages += build_rag_block(results)
    messages.append({"role": "user", "content": question})

    # ── Generate (tuned params, passed explicitly — not inherited) ───────────────
    params = {
        "model": os.environ.get("JARVIS_HELP_MODEL") or _CLOUD_DEFAULT,
        "temperature": _f("JARVIS_HELP_TEMPERATURE", 0.2),
        "max_tokens": _i("JARVIS_HELP_MAX_TOKENS", 800),
    }
    try:
        gateway = LLMGateway(make_engine("openrouter"))
        text = gateway.complete(messages, params, label="help").text.strip()
    except Exception as exc:  # noqa: BLE001 — model unreachable → clean error
        raise HelpError(f"generation failed: {exc}") from exc

    # ── Mandatory citations appendix (verbatim sources + quotes) ─────────────────
    appendix = build_citations(results, text, question)
    if appendix:
        text = f"{strip_trailing_citations(text)}\n\n{appendix}"

    return {
        "answer": text,
        "sources": _sources(results),
        "branch": branch,
        "grounded": True,
        "notice": f"grounded in {len(results)} excerpt(s) from '{index_name}'",
    }
