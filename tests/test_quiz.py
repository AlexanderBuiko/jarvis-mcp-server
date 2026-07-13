"""Tests for the quiz pool logic, the bot flow, and the upload endpoint."""

import os
from unittest import mock

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from time_server import quiz, quiz_bot


def _pool(n=6):
    return [
        {"id": f"q{i}", "topic": "coroutines", "question": f"Question {i}?",
         "options": ["a", "b", "c", "d"], "correct_index": i % 4}
        for i in range(n)
    ]


# ── pool validation / selection ──────────────────────────────────────────────


def test_validate_accepts_good_pool():
    assert quiz.validate_pool(_pool()) == []


def test_validate_rejects_bad():
    assert quiz.validate_pool([])
    assert quiz.validate_pool([{"id": "q", "topic": "t", "question": "q",
                                "options": ["a", "b"], "correct_index": 0}])
    assert quiz.validate_pool([{"id": "q", "topic": "t", "question": "q",
                                "options": ["a", "b", "c", "d"], "correct_index": 9}])


def test_select_is_deterministic_for_a_seed():
    pool = _pool(20)
    a = quiz.select_questions(pool, 5, seed=42)
    b = quiz.select_questions(pool, 5, seed=42)
    c = quiz.select_questions(pool, 5, seed=43)
    assert [q["id"] for q in a] == [q["id"] for q in b]
    assert [q["id"] for q in a] != [q["id"] for q in c]
    assert len(a) == 5


def test_select_caps_at_pool_size():
    assert len(quiz.select_questions(_pool(3), 5, seed=1)) == 3


def test_save_and_load_roundtrip(tmp_path):
    with mock.patch.dict(os.environ, {"QUIZ_POOL_PATH": str(tmp_path / "pool.json")}):
        quiz.save_pool(_pool(4))
        assert len(quiz.load_pool()) == 4


# ── bot flow (mocked Telegram) ───────────────────────────────────────────────


def _bot(tmp_path, calls):
    os.environ["QUIZ_POOL_PATH"] = str(tmp_path / "pool.json")
    quiz.save_pool(_pool(6))
    bot = quiz_bot.QuizBot(token="T", per_round=3)
    bot._allowed = {"111"}
    # Record every Telegram call instead of hitting the network.
    def rec(token, method, params, timeout=30):
        calls.append((method, params))
        return {}
    return bot, rec


def test_start_sends_intro_and_first_question(tmp_path):
    calls = []
    bot, rec = _bot(tmp_path, calls)
    with mock.patch.object(quiz_bot, "_call_telegram", side_effect=rec):
        bot.handle_update({"update_id": 1, "message": {
            "from": {"id": 111}, "chat": {"id": 111}, "text": "/start"}})
    methods = [m for m, _ in calls]
    assert methods.count("sendMessage") == 2                 # intro + Q1
    q1 = calls[-1][1]
    # Four compact letter buttons in one row; options are lettered in the text.
    kb = q1["reply_markup"]["inline_keyboard"]
    assert sum(len(row) for row in kb) == 4
    assert [b["text"] for row in kb for b in row] == ["A", "B", "C", "D"]
    assert "A) " in q1["text"] and "D) " in q1["text"]


def test_non_allowlisted_user_is_ignored(tmp_path):
    calls = []
    bot, rec = _bot(tmp_path, calls)
    with mock.patch.object(quiz_bot, "_call_telegram", side_effect=rec):
        bot.handle_update({"update_id": 1, "message": {
            "from": {"id": 999}, "chat": {"id": 999}, "text": "/start"}})
    assert calls == []                                       # silently ignored


def test_full_round_scores_and_reports(tmp_path):
    calls = []
    bot, rec = _bot(tmp_path, calls)
    with mock.patch.object(quiz_bot, "_call_telegram", side_effect=rec):
        bot.handle_update({"update_id": 1, "message": {
            "from": {"id": 111}, "chat": {"id": 111}, "text": "/start"}})
        session = bot._sessions["111"]
        # Answer all 3 questions correctly by reading the loaded session.
        for i in range(3):
            correct = session["questions"][i]["correct_index"]
            bot.handle_update({"update_id": 2 + i, "callback_query": {
                "id": f"c{i}", "from": {"id": 111},
                "message": {"chat": {"id": 111}}, "data": f"{i}:{correct}"}})
    result = calls[-1][1]["text"]
    assert "3/3" in result
    assert "111" not in bot._sessions                        # session cleared after round


def test_missed_round_appends_model_advice(tmp_path):
    calls = []
    bot, rec = _bot(tmp_path, calls)
    with mock.patch.object(quiz_bot, "_call_telegram", side_effect=rec), \
         mock.patch.object(quiz_bot, "generate_advice", return_value="Review coroutine scopes."):
        bot.handle_update({"update_id": 1, "message": {
            "from": {"id": 111}, "chat": {"id": 111}, "text": "/start"}})
        session = bot._sessions["111"]
        for i in range(3):
            wrong = (session["questions"][i]["correct_index"] + 1) % 4  # always wrong
            bot.handle_update({"update_id": 2 + i, "callback_query": {
                "id": f"c{i}", "from": {"id": 111},
                "message": {"chat": {"id": 111}}, "data": f"{i}:{wrong}"}})
    result = calls[-1][1]["text"]
    assert "0/3" in result
    assert "What to focus on next time" in result
    assert "Review coroutine scopes." in result


def test_perfect_round_makes_no_advice_call(tmp_path):
    # All correct → no missed items → generate_advice returns "" without an LLM call.
    calls = []
    bot, rec = _bot(tmp_path, calls)
    with mock.patch.object(quiz_bot, "_call_telegram", side_effect=rec), \
         mock.patch("time_server.llm_proxy._forward_to_ollama") as fwd:
        bot.handle_update({"update_id": 1, "message": {
            "from": {"id": 111}, "chat": {"id": 111}, "text": "/start"}})
        s = bot._sessions["111"]
        for i in range(3):
            c = s["questions"][i]["correct_index"]
            bot.handle_update({"update_id": 2 + i, "callback_query": {
                "id": f"c{i}", "from": {"id": 111},
                "message": {"chat": {"id": 111}}, "data": f"{i}:{c}"}})
    fwd.assert_not_called()
    assert "3/3" in calls[-1][1]["text"]


def test_stale_tap_is_ignored_gracefully(tmp_path):
    calls = []
    bot, rec = _bot(tmp_path, calls)
    with mock.patch.object(quiz_bot, "_call_telegram", side_effect=rec):
        bot.handle_update({"update_id": 1, "message": {
            "from": {"id": 111}, "chat": {"id": 111}, "text": "/start"}})
        before = len(calls)
        # A tap for question index 2 while the bot is on question 0 → only an ack.
        bot.handle_update({"update_id": 2, "callback_query": {
            "id": "c", "from": {"id": 111}, "message": {"chat": {"id": 111}}, "data": "2:0"}})
    assert calls[before][0] == "answerCallbackQuery"
    assert len(calls) == before + 1                          # no new question sent


# ── upload endpoint ──────────────────────────────────────────────────────────


def _upload_client():
    from time_server.server import _quiz_upload
    app = Starlette(routes=[Route("/quiz/pool", _quiz_upload, methods=["POST"])])
    return TestClient(app)


def test_upload_stores_valid_pool(tmp_path):
    with mock.patch.dict(os.environ, {"QUIZ_POOL_PATH": str(tmp_path / "pool.json")}):
        r = _upload_client().post("/quiz/pool", json=_pool(5))
        assert r.status_code == 200
        assert r.json()["count"] == 5
        assert len(quiz.load_pool()) == 5


def test_upload_rejects_invalid_pool(tmp_path):
    with mock.patch.dict(os.environ, {"QUIZ_POOL_PATH": str(tmp_path / "pool.json")}):
        r = _upload_client().post("/quiz/pool", json=[{"id": "q", "topic": "t"}])
        assert r.status_code == 422
        assert "details" in r.json()
