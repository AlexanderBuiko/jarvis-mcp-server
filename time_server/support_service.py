"""
Support "brain" — answers a product question grounded in the FAQ **and** the user's
ticket/CRM context.

The support counterpart to help_service / review_service: given a question and an
optional ticket or user id, it pulls that context from the CRM (``crm.py``), retrieves
relevant FAQ chunks (RAG), and composes an answer tailored to the user's situation.
The ticket/user data reaches the assistant from the same CRM that is exposed over MCP
(``jarvis.get_ticket`` etc.), so this is the server-side twin of "connect your CRM".

Reuses jarvis-cli's primitives (retrieval, the grounded prompt block, citations, the
LLM gateway), like the other endpoints.

Agent tuning (support role, not defaults): low-ish temperature (helpful but grounded),
a `direct` answer that cites the FAQ yet always uses the ticket specifics, and a bounded
length. Unknown ticket/user degrades gracefully (answer from the FAQ, note the miss).

Config (env):
    JARVIS_SUPPORT_INDEX       FAQ index to answer from     (default "support-faq")
    JARVIS_SUPPORT_K           chunks retrieved             (default 4)
    JARVIS_SUPPORT_MODEL       generation model             (default: OpenRouter default)
    JARVIS_SUPPORT_TEMPERATURE sampling temperature         (default 0.2)
    JARVIS_SUPPORT_MAX_TOKENS  max answer tokens            (default 700)
    JARVIS_INDEX_DIR           where indexes live           (default ~/.jarvis/indexes)
    JARVIS_CRM_PATH            CRM JSON path (see crm.py)
"""

from __future__ import annotations

import os
import re

from jarvis.indexing import IndexPipeline, IndexStore, make_embedder
from jarvis.prompt_builder.builder import build_rag_block
from jarvis.rag.cite import build_citations, strip_trailing_citations
from jarvis.llm.gateway import LLMGateway
from jarvis.llm.router import make_engine
from jarvis.openrouter.client import DEFAULT_MODEL as _CLOUD_DEFAULT

from . import crm


class SupportError(Exception):
    """A caller-facing failure (bad request / unavailable retrieval)."""


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


_SYSTEM = (
    "You are a friendly, precise product support assistant. Answer the user's question "
    "using the FAQ excerpts provided in this turn, citing them inline as [n]. When a "
    "support context (the user's ticket, plan, environment, error) is given, TAILOR the "
    "answer to it — name the specific error and situation and give concrete next steps "
    "for THIS user. If the FAQ doesn't cover the question, say so plainly, but still "
    "help with what the ticket tells you. Be concise and actionable."
)


def _resolve_context(ticket_id: str | None, user_id: str | None) -> tuple[dict | None, dict | None, list[str]]:
    """Look up the ticket and/or user from the CRM. Returns (ticket, user, notes).

    Unknown ids are not fatal — they're recorded in ``notes`` so the answer can note
    the miss and still help from the FAQ.
    """
    notes: list[str] = []
    ticket = user = None
    if ticket_id:
        ticket = crm.get_ticket(ticket_id)
        if ticket is None:
            notes.append(f"no ticket '{ticket_id}' in the CRM")
        else:
            user = crm.get_user(ticket.get("user_id", ""))
    if user is None and user_id:
        user = crm.get_user(user_id)
        if user is None:
            notes.append(f"no user '{user_id}' in the CRM")
    return ticket, user, notes


def _context_block(ticket: dict | None, user: dict | None) -> list[dict]:
    """A pseudo-exchange carrying the CRM context, injected ahead of the question."""
    if not ticket and not user:
        return []
    lines = ["[Support context — the person you are helping]"]
    if user:
        lines.append(
            f"User: {user.get('name', '?')} (plan: {user.get('plan', '?')}; "
            f"environment: {user.get('environment', 'n/a')})"
        )
    if ticket:
        lines.append(
            f"Ticket {ticket.get('id', '?')} [{ticket.get('status', '?')}, "
            f"priority={ticket.get('priority', '?')}, area={ticket.get('product_area', '?')}]: "
            f"{ticket.get('subject', '')}"
        )
        if ticket.get("description"):
            lines.append(f"  Description: {ticket['description']}")
        if ticket.get("error"):
            lines.append(f"  Error: {ticket['error']}")
    return [
        {"role": "user", "content": "\n".join(lines)},
        {"role": "assistant", "content": "Understood — I'll tailor my answer to this user's situation."},
    ]


# Inline citation markers the model writes ("[1]", "[2, 3]"). Stripped in plain style.
_INLINE_CITE = re.compile(r"[ \t]*\[\d+(?:\s*,\s*\d+)*\]")


def _strip_inline_citations(text: str) -> str:
    return _INLINE_CITE.sub("", text)


def _retrieval_query(question: str, ticket: dict | None) -> str:
    """Focus retrieval with the ticket's subject / area / error when present."""
    if not ticket:
        return question
    extra = " ".join(str(ticket.get(k, "")) for k in ("subject", "product_area", "error"))
    return f"{question} {extra}".strip()


def answer(
    question: str,
    ticket_id: str | None = None,
    user_id: str | None = None,
    style: str | None = None,
    history: list[dict] | None = None,
    min_score: float | None = None,
) -> dict:
    """Answer a support question grounded in the FAQ and the CRM ticket/user context.

    ``style`` selects the presentation: ``"plain"`` (default) returns clean prose with
    the inline ``[n]`` markers stripped and no Sources/Quotes block; ``"debug"`` keeps
    the markers and appends the verbatim citations (for analyzing retrieval). The
    ``sources`` list is returned either way. ``history`` is prior ``{role, content}``
    turns, injected before the question so multi-turn follow-ups stay coherent.

    Returns ``{answer, sources, ticket, user, notice}``. Raises ``SupportError`` for
    bad input or an unavailable FAQ index.
    """
    style = (style or "plain").lower()
    question = (question or "").strip()
    if not question:
        raise SupportError("question must be a non-empty string")

    ticket, user, notes = _resolve_context(ticket_id, user_id)

    index_name = os.environ.get("JARVIS_SUPPORT_INDEX", "support-faq").strip() or "support-faq"
    store = IndexStore()
    header = store.load_header(index_name)
    if header is None:
        raise SupportError(
            f"FAQ index '{index_name}' not found. Build it (index build faq "
            f"name={index_name}) and point JARVIS_INDEX_DIR at it."
        )
    try:
        embedder = make_embedder(header.get("provider"), header.get("model"))
        results = IndexPipeline(embedder, store).search(
            index_name, _retrieval_query(question, ticket), _i("JARVIS_SUPPORT_K", 4)
        )
    except Exception as exc:  # noqa: BLE001 — retrieval unreachable → clean error
        raise SupportError(f"FAQ retrieval failed: {exc}") from exc

    # Answerability gate: when ``min_score`` is given and nothing clears the relevance
    # bar, report we can't answer (the support bot uses this to log the request for the
    # developers instead of guessing). Off by default (endpoint stays backward-compatible).
    best = max((r.get("score", 0.0) for r in results), default=0.0)
    if min_score is not None and (not results or best < min_score):
        return {
            "answer": "", "answered": False, "sources": [],
            "ticket": {k: ticket.get(k) for k in ("id", "subject", "status", "product_area")} if ticket else None,
            "user": {k: user.get(k) for k in ("id", "name", "plan")} if user else None,
            "notice": f"no FAQ cleared the relevance bar (best {best:.2f} < {min_score:.2f})",
        }

    messages = [{"role": "system", "content": _SYSTEM}]
    messages += build_rag_block(results)
    messages += _context_block(ticket, user)
    for turn in (history or []):
        role = turn.get("role", "user")
        content = turn.get("content", "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    params = {
        "model": os.environ.get("JARVIS_SUPPORT_MODEL") or _CLOUD_DEFAULT,
        "temperature": _f("JARVIS_SUPPORT_TEMPERATURE", 0.2),
        "max_tokens": _i("JARVIS_SUPPORT_MAX_TOKENS", 700),
    }
    try:
        text = LLMGateway(make_engine("openrouter")).complete(messages, params, label="support").text.strip()
    except Exception as exc:  # noqa: BLE001 — model unreachable → clean error
        raise SupportError(f"generation failed: {exc}") from exc

    if style == "debug":
        # Keep the [n] markers and append the verbatim Sources + Quotes.
        appendix = build_citations(results, text, question)
        if appendix:
            text = f"{strip_trailing_citations(text)}\n\n{appendix}"
    else:
        # Plain (default): clean prose — drop any model-written citation block and the
        # inline [n] markers.
        text = _strip_inline_citations(strip_trailing_citations(text)).strip()

    sources: list[str] = []
    for r in results:
        fn = (r.get("metadata") or {}).get("filename", "?")
        if fn not in sources:
            sources.append(fn)

    notice = f"grounded in {len(results)} FAQ excerpt(s) from '{index_name}'"
    if ticket:
        notice += f"; ticket {ticket.get('id')}"
    if notes:
        notice += " — " + "; ".join(notes)

    return {
        "answer": text,
        "answered": True,
        "sources": sources,
        "ticket": {k: ticket.get(k) for k in ("id", "subject", "status", "product_area")} if ticket else None,
        "user": {k: user.get(k) for k in ("id", "name", "plan")} if user else None,
        "notice": notice,
    }
