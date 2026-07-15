"""
PR-review "brain" — RAG-grounded code review of a diff, server-side.

The reactive counterpart to `help_service`: a GitHub Action fetches a PR's diff and
changed files and POSTs them here; this returns a structured review (potential bugs /
architectural problems / recommendations) grounded in the project's own code and docs.
The Action posts the review back to the PR — this service holds no GitHub credentials.

Like `help_service`, it reuses jarvis-cli's primitives rather than duplicating them:
retrieval (`jarvis.indexing`), the grounded prompt block
(`jarvis.prompt_builder.build_rag_block`), and the LLM gateway (`jarvis.llm`).

Agent tuning (for a consistent, thorough reviewer — not defaults): low temperature,
a `direct` call with a strongly-structured three-section prompt, bounded output. One
retry on transient LLM failure; latency + token usage are returned so the Action can
track cost/latency metrics.

Config (env):
    JARVIS_REVIEW_INDEXES     comma list to retrieve from  (default "jarvis-code,jarvis-docs")
    JARVIS_REVIEW_K           chunks per index             (default 4)
    JARVIS_REVIEW_TOP_N       chunks kept overall          (default 8)
    JARVIS_REVIEW_MODEL       generation model             (default: OpenRouter default)
    JARVIS_REVIEW_TEMPERATURE sampling temperature         (default 0.1)
    JARVIS_REVIEW_MAX_TOKENS  max review tokens            (default 1500)
    JARVIS_REVIEW_QUERY_CHARS diff chars used for retrieval(default 2000)
    JARVIS_INDEX_DIR          where indexes live           (default ~/.jarvis/indexes)
"""

from __future__ import annotations

import os

from jarvis.indexing import IndexPipeline, IndexStore, make_embedder
from jarvis.prompt_builder.builder import build_rag_block
from jarvis.llm.gateway import LLMGateway
from jarvis.llm.router import make_engine
from jarvis.openrouter.client import DEFAULT_MODEL as _CLOUD_DEFAULT


class ReviewError(Exception):
    """A caller-facing failure (bad request / retrieval or model unavailable)."""


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
    "You are a senior code reviewer for THIS project. Review the pull-request diff "
    "below. Ground your review in the project's conventions and existing code, given "
    "as knowledge-base excerpts — prefer them over generic advice, and cite an excerpt "
    "as [n] or name a file path when relevant.\n\n"
    "Use exactly these three sections, with these headings:\n"
    "### Potential bugs\n"
    "### Architectural problems\n"
    "### Recommendations\n\n"
    "Be specific and concise; reference the file/function names from the diff. If a "
    "section has nothing to report, write 'None found.' Judge only what the diff "
    "changes. End with a final line exactly of the form 'VERDICT: approve' if you "
    "found no blocking problems, otherwise 'VERDICT: comment'."
)


def _indexes() -> list[str]:
    raw = os.environ.get("JARVIS_REVIEW_INDEXES", "jarvis-code,jarvis-docs")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _retrieve(query: str) -> list[dict]:
    """Retrieve related chunks across the configured indexes (code + docs).

    Each index embeds the query with its own recorded provider/model, so a mixed
    fleet (e.g. a code index and a docs index) each match how they were built. A
    missing or unreachable index is skipped, not fatal — the review degrades to less
    context rather than failing.
    """
    store = IndexStore()
    k = _i("JARVIS_REVIEW_K", 4)
    top_n = _i("JARVIS_REVIEW_TOP_N", 8)
    merged: list[dict] = []
    for name in _indexes():
        header = store.load_header(name)
        if header is None:
            continue
        try:
            embedder = make_embedder(header.get("provider"), header.get("model"))
            merged.extend(IndexPipeline(embedder, store).search(name, query, k))
        except Exception:  # noqa: BLE001 — one bad index shouldn't sink the review
            continue
    merged.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return merged[:top_n]


def _sources(results: list[dict]) -> list[str]:
    seen: list[str] = []
    for r in results:
        fn = (r.get("metadata") or {}).get("filename", "?")
        if fn not in seen:
            seen.append(fn)
    return seen


def _generate(messages: list[dict], params: dict):
    """Generate with one retry on transient failure. Returns the Completion.

    Raises ReviewError only after both attempts fail, so the route returns a clean
    error instead of a 500 — and the Action can decide whether to skip commenting.
    """
    gateway = LLMGateway(make_engine("openrouter"))
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            return gateway.complete(messages, params, label="review"), attempt
        except Exception as exc:  # noqa: BLE001 — retry then surface
            last_exc = exc
    raise ReviewError(f"generation failed after retries: {last_exc}")


def review(diff: str, changed_files: list[str] | None = None, repo: str | None = None) -> dict:
    """Produce a structured, grounded review of a PR diff.

    Returns ``{review, verdict, findings_present, sources, model, usage}``. Raises
    ``ReviewError`` for bad input or unavailable generation.
    """
    diff = (diff or "").strip()
    if not diff:
        raise ReviewError("diff must be a non-empty string")
    changed_files = changed_files or []

    # Retrieval query: the changed paths plus a bounded slice of the diff (embedding
    # the whole diff would be large and noisy).
    query_chars = _i("JARVIS_REVIEW_QUERY_CHARS", 2000)
    query = ("Files changed: " + ", ".join(changed_files) + "\n" + diff)[:query_chars]
    results = _retrieve(query)

    file_list = "\n".join(f"- {f}" for f in changed_files) or "- (not provided)"
    user = f"Changed files:\n{file_list}\n\nDiff:\n```diff\n{diff}\n```"
    messages = [{"role": "system", "content": _SYSTEM}]
    messages += build_rag_block(results)
    messages.append({"role": "user", "content": user})

    params = {
        "model": os.environ.get("JARVIS_REVIEW_MODEL") or _CLOUD_DEFAULT,
        "temperature": _f("JARVIS_REVIEW_TEMPERATURE", 0.1),
        "max_tokens": _i("JARVIS_REVIEW_MAX_TOKENS", 1500),
    }
    completion, retries = _generate(messages, params)
    text = (completion.text or "").strip()

    # Parse the trailing VERDICT line (drives the non-blocking-comment vs blocking
    # decision; kept out of the posted body).
    verdict = "comment"
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().upper().startswith("VERDICT:"):
            verdict = lines[i].split(":", 1)[1].strip().lower() or "comment"
            text = "\n".join(lines[:i]).rstrip()
            break

    usage = (completion.response or {}).get("usage") or {}
    return {
        "review": text,
        "verdict": verdict,
        "findings_present": verdict != "approve",
        "sources": _sources(results),
        "model": params["model"],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "latency_ms": round(completion.latency_ms, 1),
            "retries": retries,
        },
    }
