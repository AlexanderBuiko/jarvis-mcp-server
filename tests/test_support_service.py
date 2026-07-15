"""Tests for the support brain (time_server/support_service.py).

Offline: a FakeEmbedder FAQ index + a fake engine; the CRM uses the bundled JSON
(T-1002 exists). Covers with-ticket / no-ticket / bad-ticket / empty / missing-index.
"""

import pytest

from jarvis.indexing import IndexPipeline, make_embedder

from time_server import support_service


class _FakeCompletion:
    def __init__(self, text):
        self.text = text
        self.tool_calls = None


class _FakeEngine:
    def complete(self, messages, params):
        return _FakeCompletion("Set MCP_API_KEY to match the server [1].")

    def get_pricing(self, model_id):
        return (None, None)

    def get_context_window(self, model_id):
        return None


@pytest.fixture
def faq_index(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("JARVIS_SUPPORT_INDEX", "t-faq")
    faq = tmp_path / "faq"
    faq.mkdir()
    (faq / "auth.md").write_text(
        "# Authorization\n\nA 401 unauthorized means the MCP_API_KEY is missing or wrong.\n"
    )
    IndexPipeline(make_embedder("fake")).build(str(faq), "t-faq", strategy="structure")
    monkeypatch.setattr(support_service, "make_engine", lambda provider: _FakeEngine())
    return faq


def test_answer_with_ticket_plain_by_default(faq_index):
    result = support_service.answer("Why isn't authorization working?", ticket_id="T-1002")
    assert result["ticket"]["id"] == "T-1002"
    assert result["user"]["id"] == "U-1"           # ticket → its user, resolved via CRM
    assert result["sources"]                        # sources always in the payload
    assert "Sources:" not in result["answer"]      # ...but not in the plain answer text
    assert "[1]" not in result["answer"]           # inline markers stripped
    assert "ticket T-1002" in result["notice"]


def test_answer_debug_style_has_sources(faq_index):
    result = support_service.answer(
        "Why isn't authorization working?", ticket_id="T-1002", style="debug"
    )
    assert "Sources:" in result["answer"]          # debug appends the citations


def test_answer_with_history(faq_index):
    history = [
        {"role": "user", "content": "I deployed to Cloud Run."},
        {"role": "assistant", "content": "Got it — what error do you see?"},
    ]
    result = support_service.answer("It says 401.", ticket_id="T-1002", history=history)
    assert result["answer"]                          # multi-turn call succeeds


def test_answer_no_ticket(faq_index):
    result = support_service.answer("How do I fix a 401?")
    assert result["ticket"] is None
    assert result["user"] is None
    assert result["sources"]


def test_answer_bad_ticket_degrades(faq_index):
    result = support_service.answer("help me", ticket_id="T-9999")
    assert result["ticket"] is None
    assert "no ticket 'T-9999'" in result["notice"]


def test_answer_min_score_gate(faq_index):
    # An impossible bar → nothing clears it → answered False (bot logs it for devs).
    result = support_service.answer("Why isn't auth working?", ticket_id="T-1002", min_score=1.5)
    assert result["answered"] is False
    assert result["answer"] == ""


def test_answer_answered_flag_true(faq_index):
    result = support_service.answer("Why isn't auth working?", ticket_id="T-1002")
    assert result["answered"] is True


def test_answer_empty_question(faq_index):
    with pytest.raises(support_service.SupportError):
        support_service.answer("   ")


def test_answer_missing_index(faq_index, monkeypatch):
    monkeypatch.setenv("JARVIS_SUPPORT_INDEX", "does-not-exist")
    with pytest.raises(support_service.SupportError):
        support_service.answer("anything", ticket_id="T-1002")
