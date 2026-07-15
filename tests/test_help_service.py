"""Tests for the server-side project-help brain (time_server/help_service.py).

Uses the deterministic FakeEmbedder (no network) for retrieval and a fake engine
for generation, so the grounded / closed-domain / missing-index paths are covered
offline.
"""

import pytest

from jarvis.indexing import IndexPipeline, make_embedder

from time_server import help_service


class _FakeCompletion:
    def __init__(self, text):
        self.text = text
        self.tool_calls = None


class _FakeEngine:
    def complete(self, messages, params):
        return _FakeCompletion("Chunking splits markdown by headings [1].")

    def get_pricing(self, model_id):
        return (None, None)

    def get_context_window(self, model_id):
        return None


@pytest.fixture
def fake_index(tmp_path, monkeypatch):
    """Build a tiny fake-embedded index and point the service at it."""
    monkeypatch.setenv("JARVIS_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("JARVIS_HELP_INDEX", "t-index")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "chunking.md").write_text(
        "# Chunking\n\nStructure-aware chunking splits Markdown by headings.\n"
    )
    (docs / "providers.md").write_text(
        "# Providers\n\nProviders include openrouter (cloud) and ollama (local).\n"
    )
    IndexPipeline(make_embedder("fake")).build(str(docs), "t-index", strategy="structure")
    # Generation → fake engine (no OpenRouter needed).
    monkeypatch.setattr(help_service, "make_engine", lambda provider: _FakeEngine())
    return docs


def test_answer_grounded(fake_index, monkeypatch):
    monkeypatch.setenv("JARVIS_HELP_MIN_SCORE", "-1.0")  # always clear the bar
    result = help_service.answer("How does chunking work?", branch="main")
    assert result["grounded"] is True
    assert result["branch"] == "main"
    assert result["sources"]
    assert "Chunking splits markdown by headings" in result["answer"]
    # The mandatory citation appendix is appended.
    assert "Sources:" in result["answer"]


def test_answer_closed_domain_declines(fake_index, monkeypatch):
    monkeypatch.setenv("JARVIS_HELP_MIN_SCORE", "0.999")  # nothing clears the bar
    result = help_service.answer("What is the best tiramisu recipe?")
    assert result["grounded"] is False
    assert result["sources"] == []
    assert "I don't know" in result["answer"]


def test_answer_empty_question(fake_index):
    with pytest.raises(help_service.HelpError):
        help_service.answer("   ")


def test_answer_missing_index(fake_index, monkeypatch):
    monkeypatch.setenv("JARVIS_HELP_INDEX", "does-not-exist")
    with pytest.raises(help_service.HelpError):
        help_service.answer("anything")
