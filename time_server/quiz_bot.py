"""
Private Telegram quiz bot.

A background daemon thread long-polls Telegram (`getUpdates`), and runs a small
multiple-choice quiz from the uploaded pool:

    /start → a time-seeded round of N questions, each with 4 inline-keyboard
    buttons → the bot scores each tap → after the round, a result message
    (score + which ones were missed and their correct answer).

Private by construction: it only responds to allow-listed Telegram user ids and
silently ignores everyone else. It reuses TELEGRAM_BOT_TOKEN (the send-only alert
path never polls, so there is no getUpdates conflict). No knowledge base is
involved — it serves generated MCQs from the pool only.

Config (env):
    TELEGRAM_BOT_TOKEN       the bot token (shared with the alert sender)
    QUIZ_ALLOWED_USER_IDS    comma-separated Telegram user ids allowed to play
                             (defaults to TELEGRAM_CHAT_ID)
    QUIZ_QUESTIONS_PER_ROUND questions per /start (default 5)
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import urllib.parse
import urllib.request

from . import quiz

logger = logging.getLogger("time_server.quiz_bot")

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - environment-dependent
    _SSL_CTX = None

_API = "https://api.telegram.org/bot{token}/{method}"


def _allowed_ids() -> set[str]:
    raw = os.environ.get("QUIZ_ALLOWED_USER_IDS") or os.environ.get("TELEGRAM_CHAT_ID", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_configured() -> bool:
    """True when a bot token and at least one allow-listed user id are present."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and _allowed_ids())


def _call_telegram(token: str, method: str, params: dict, timeout: float = 30) -> dict:
    """One Telegram Bot API call (urllib). Returns the parsed body, or {} on error."""
    url = _API.format(token=token, method=method)
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — a bot call must never crash the thread
        logger.warning("telegram %s failed: %s", method, exc)
        return {}


def build_keyboard(qindex: int, options: list[str]) -> dict:
    """Inline keyboard: one button per option; callback data carries q + option index."""
    return {"inline_keyboard": [
        [{"text": opt, "callback_data": f"{qindex}:{i}"}] for i, opt in enumerate(options)
    ]}


def format_result(session: dict) -> str:
    """Score line plus each missed question and its correct answer."""
    total = len(session["questions"])
    lines = [f"🏁 Result: {session['score']}/{total} correct."]
    if session["wrong"]:
        lines.append("\nReview:")
        for q, correct in session["wrong"]:
            lines.append(f"• {q}\n   ✅ {correct}")
    else:
        lines.append("Perfect round! 🎉")
    return "\n".join(lines)


class QuizBot:
    """Long-polls Telegram and runs the quiz for allow-listed users."""

    def __init__(self, token: str | None = None, per_round: int | None = None) -> None:
        self._token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self._allowed = _allowed_ids()
        self._per_round = per_round or quiz.QUESTIONS_PER_ROUND
        self._sessions: dict[str, dict] = {}   # chat_id -> round state
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="quiz-bot", daemon=True)
        self._thread.start()
        logger.info("quiz bot started (allowed users: %d)", len(self._allowed))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── poll loop ──
    def _run(self) -> None:
        while not self._stop.is_set():
            body = _call_telegram(self._token, "getUpdates",
                                  {"offset": self._offset, "timeout": 25}, timeout=30)
            for update in body.get("result", []):
                self._offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as exc:  # noqa: BLE001 — one bad update mustn't stop the bot
                    logger.warning("quiz update failed: %s", exc)

    # ── dispatch (pure enough to unit-test with a fake _call_telegram) ──
    def handle_update(self, update: dict) -> None:
        if "message" in update:
            msg = update["message"]
            user = str((msg.get("from") or {}).get("id", ""))
            if not self._is_allowed(user):
                return
            text = (msg.get("text") or "").strip()
            chat_id = str(msg["chat"]["id"])
            if text.split()[0:1] == ["/start"]:
                self._begin(chat_id)
        elif "callback_query" in update:
            cb = update["callback_query"]
            user = str((cb.get("from") or {}).get("id", ""))
            if not self._is_allowed(user):
                return
            self._on_answer(cb)

    def _is_allowed(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self._allowed

    def _begin(self, chat_id: str) -> None:
        pool = quiz.load_pool()
        questions = quiz.select_questions(pool, self._per_round)
        if not questions:
            self._send(chat_id, "No quiz available yet — the question pool is empty.")
            return
        self._sessions[chat_id] = {"questions": questions, "idx": 0, "score": 0, "wrong": []}
        self._send(chat_id, f"🧠 Android/Kotlin quiz — {len(questions)} questions. Good luck!")
        self._send_question(chat_id)

    def _send_question(self, chat_id: str) -> None:
        s = self._sessions[chat_id]
        i = s["idx"]
        q = s["questions"][i]
        self._send(chat_id, f"Q{i + 1}. {q['question']}", build_keyboard(i, q["options"]))

    def _on_answer(self, cb: dict) -> None:
        chat_id = str(cb["message"]["chat"]["id"])
        s = self._sessions.get(chat_id)
        cb_id = cb.get("id", "")
        try:
            qidx, opt = (int(x) for x in cb["data"].split(":"))
        except (KeyError, ValueError):
            self._ack(cb_id)
            return
        # Ignore stale taps (a button from a question that isn't the current one).
        if not s or qidx != s["idx"]:
            self._ack(cb_id)
            return

        q = s["questions"][qidx]
        correct = opt == q["correct_index"]
        if correct:
            s["score"] += 1
            self._ack(cb_id, "✅ Correct")
        else:
            s["wrong"].append((q["question"], q["options"][q["correct_index"]]))
            self._ack(cb_id, "❌ Incorrect")

        s["idx"] += 1
        if s["idx"] < len(s["questions"]):
            self._send_question(chat_id)
        else:
            self._send(chat_id, format_result(s))
            self._sessions.pop(chat_id, None)

    # ── telegram helpers ──
    def _send(self, chat_id: str, text: str, keyboard: dict | None = None) -> None:
        params: dict = {"chat_id": chat_id, "text": text}
        if keyboard:
            params["reply_markup"] = keyboard
        _call_telegram(self._token, "sendMessage", params)

    def _ack(self, callback_id: str, text: str = "") -> None:
        if callback_id:
            _call_telegram(self._token, "answerCallbackQuery",
                          {"callback_query_id": callback_id, "text": text})
