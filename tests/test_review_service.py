"""Tests for the PR-review brain (time_server/review_service.py).

Offline: a deterministic FakeEmbedder index + a fake engine, so retrieval, verdict
parsing, retry/fallback, and the empty-diff guard are covered without network.
"""

import pytest

from jarvis.indexing import IndexPipeline, make_embedder

from time_server import review_service


class _FakeCompletion:
    def __init__(self, text):
        self.text = text
        self.tool_calls = None
        self.latency_ms = 12.3
        self.response = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


_APPROVE = (
    "### Potential bugs\nNone found.\n"
    "### Architectural problems\nNone found.\n"
    "### Recommendations\nLooks consistent with the code.\n"
    "VERDICT: approve"
)


class _FakeEngine:
    def __init__(self, fail_times=0, text=_APPROVE):
        self.calls = 0
        self.fail_times = fail_times
        self.text = text

    def complete(self, messages, params):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("transient boom")
        return _FakeCompletion(self.text)

    def get_pricing(self, model_id):
        return (None, None)

    def get_context_window(self, model_id):
        return None


@pytest.fixture
def code_index(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("JARVIS_REVIEW_INDEXES", "t-code")
    src = tmp_path / "src"
    src.mkdir()
    (src / "store.py").write_text(
        "def cosine_top_k(records, qv, k=5):\n"
        "    # query vector is normalized here; embeddings normalized at write time\n"
        "    return sorted(records, key=lambda r: r['score'])[:k]\n"
    )
    IndexPipeline(make_embedder("fake")).build(str(src), "t-code", strategy="fixed",
                                               suffixes=frozenset({".py"}))
    return src


_DIFF = "diff --git a/store.py b/store.py\n-    qv = normalize(query_vector)\n+    qv = query_vector\n"


def test_review_grounded_and_parsed(code_index, monkeypatch):
    monkeypatch.setattr(review_service, "make_engine", lambda provider: _FakeEngine())
    result = review_service.review(_DIFF, ["store.py"], repo="me/proj")
    assert result["verdict"] == "approve"
    assert result["findings_present"] is False
    assert "VERDICT" not in result["review"]          # trailing verdict stripped
    assert "### Potential bugs" in result["review"]
    assert result["sources"]                            # grounded in the code index
    assert result["usage"]["total_tokens"] == 15
    assert result["usage"]["retries"] == 0


def test_review_verdict_comment(code_index, monkeypatch):
    text = ("### Potential bugs\nDropped normalization.\n### Architectural problems\n"
            "None found.\n### Recommendations\nRestore it.\nVERDICT: comment")
    monkeypatch.setattr(review_service, "make_engine", lambda provider: _FakeEngine(text=text))
    result = review_service.review(_DIFF, ["store.py"])
    assert result["verdict"] == "comment"
    assert result["findings_present"] is True


def test_review_retries_then_succeeds(code_index, monkeypatch):
    engine = _FakeEngine(fail_times=1)
    monkeypatch.setattr(review_service, "make_engine", lambda provider: engine)
    result = review_service.review(_DIFF, ["store.py"])
    assert result["usage"]["retries"] == 1
    assert engine.calls == 2


def test_review_fails_after_retries(code_index, monkeypatch):
    monkeypatch.setattr(review_service, "make_engine", lambda provider: _FakeEngine(fail_times=2))
    with pytest.raises(review_service.ReviewError):
        review_service.review(_DIFF, ["store.py"])


def test_review_empty_diff(code_index):
    with pytest.raises(review_service.ReviewError):
        review_service.review("   ", [])
