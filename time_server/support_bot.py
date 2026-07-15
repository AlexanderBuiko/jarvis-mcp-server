"""
Telegram support bot — a strict, staged support conversation over the RAG+CRM brain.

Same scaffold as the quiz bot (a daemon thread long-polling ``getUpdates`` for an
allow-listed set of users, its own ``SUPPORT_BOT_TOKEN``), but it runs a deterministic
support **pipeline** rather than free chat:

    /start → IDENTIFY (who are you? — explicit email / user id / ticket, else your name)
           → PROBLEM  (describe it → answer from the FAQ+CRM, OR, if the docs don't
                        cover it, log the request for the developers)
           → ANOTHER  (another problem? loop back, or say "no")
           → RATING   (rate 1–5)
           → thank you + close the session

Identity is only ever taken from what the user states about themselves — the bot never
guesses "who you are" from your symptoms. Outside a session (before /start) it does not
answer; it asks you to /start. ``/debug`` shows the resolved user/ticket + FAQ sources.

Config (env):
    SUPPORT_BOT_TOKEN          the support bot's token (a 2nd bot in @BotFather)
    SUPPORT_ALLOWED_USER_IDS   comma-separated Telegram user ids (default TELEGRAM_CHAT_ID)
    SUPPORT_SESSION_IDLE_S     idle seconds before a session auto-resets (default 1800)
    SUPPORT_MIN_SCORE          FAQ relevance bar below which a problem is logged, not
                               answered (default 0.25)
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.request

from . import crm
from . import support_service

logger = logging.getLogger("time_server.support_bot")

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - environment-dependent
    _SSL_CTX = None

_API = "https://api.telegram.org/bot{token}/{method}"

# ── Prompts ──
_IDENTIFY_PROMPT = (
    "👋 Support session started. First, who am I helping? Send your account email, "
    "user id (e.g. U-1), or a ticket number (e.g. T-1002) — or just tell me your name."
)
_ANOTHER_PROMPT = "Is there anything else I can help with? Describe it, or reply 'no'."
_RATING_PROMPT = "Before you go — how satisfied were you with the help? Please rate 1–5."
_NEGATIVE = {"no", "nope", "nah", "n", "that's all", "thats all", "done", "nothing",
             "no thanks", "all good", "that is all", "im good", "i'm good"}

# Identity signals (only used when the user is telling us who they are).
_TICKET_RE = re.compile(r"\bT-?\d{3,}\b", re.IGNORECASE)
_USER_RE = re.compile(r"\bU-?\d+\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _allowed_ids() -> set[str]:
    raw = os.environ.get("SUPPORT_ALLOWED_USER_IDS") or os.environ.get("TELEGRAM_CHAT_ID", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_configured() -> bool:
    """True when a support bot token and at least one allow-listed user id are present."""
    return bool(os.environ.get("SUPPORT_BOT_TOKEN", "").strip() and _allowed_ids())


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


def _norm_id(prefix: str, token: str) -> str:
    """'T1002'/'T-1002' → 'T-1002'; 'U1'/'U-1' → 'U-1'."""
    return f"{prefix}-{re.sub(r'[^0-9]', '', token)}"


def resolve_identity(text: str) -> tuple[dict | None, dict | None, str | None]:
    """From a self-identification message → (user, ticket, guest_name).

    Explicit only: a ticket id (→ its user), a user id, or an email. If none resolve to
    a CRM record, returns a guest name (the text) so the person can still be greeted —
    but the bot never infers identity from a described *symptom*.
    """
    for token in _TICKET_RE.findall(text):
        ticket = crm.get_ticket(_norm_id("T", token))
        if ticket:
            return crm.get_user(ticket.get("user_id", "")), ticket, None
    for token in _USER_RE.findall(text):
        user = crm.get_user(_norm_id("U", token))
        if user:
            return user, None, None
    for email in _EMAIL_RE.findall(text):
        user = crm.find_user_by_email(email)
        if user:
            return user, None, None
    name = text.strip()
    return None, None, (name[:40] if name else None)


class SupportBot:
    """Long-polls Telegram and runs the staged support pipeline for allow-listed users."""

    def __init__(self, token: str | None = None, idle_s: float | None = None,
                 min_score: float | None = None) -> None:
        self._token = (token or os.environ.get("SUPPORT_BOT_TOKEN", "")).strip()
        self._allowed = _allowed_ids()
        self._idle = idle_s if idle_s is not None else float(
            os.environ.get("SUPPORT_SESSION_IDLE_S", "1800"))
        self._min_score = min_score if min_score is not None else float(
            os.environ.get("SUPPORT_MIN_SCORE", "0.25"))
        self._sessions: dict[str, dict] = {}
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="support-bot", daemon=True)
        self._thread.start()
        logger.info("support bot started (allowed users: %d)", len(self._allowed))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            body = _call_telegram(self._token, "getUpdates",
                                  {"offset": self._offset, "timeout": 25}, timeout=30)
            for update in body.get("result", []):
                self._offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as exc:  # noqa: BLE001 — one bad update mustn't stop the bot
                    logger.warning("support update failed: %s", exc)

    # ── dispatch ──
    def handle_update(self, update: dict) -> None:
        if "message" not in update:
            return
        msg = update["message"]
        if not self._is_allowed(str((msg.get("from") or {}).get("id", ""))):
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        chat_id = str(msg["chat"]["id"])
        cmd = text.split()[0].lower()

        if cmd in ("/start", "/new"):
            self._begin(chat_id)
            return
        if cmd == "/end":
            self._end(chat_id)
            return

        session = self._sessions.get(chat_id)
        if session and (time.time() - session.get("last_active", 0)) > self._idle:
            self._sessions.pop(chat_id, None)
            session = None

        if cmd == "/debug":
            if session:
                session["debug"] = not session["debug"]
                self._send(chat_id, "🔎 Debug ON — I'll show the resolved user/ticket + sources."
                           if session["debug"] else "Debug OFF.")
            else:
                self._send(chat_id, "Send /start to begin a support session.")
            return

        # Outside a session, the bot does not answer — it asks the user to /start.
        if session is None:
            self._send(chat_id, "Send /start to begin a support session.")
            return

        session["last_active"] = time.time()
        state = session["state"]
        if state == "identify":
            self._on_identify(chat_id, session, text)
        elif state == "problem":
            self._process_problem(chat_id, session, text)
        elif state == "another":
            self._on_another(chat_id, session, text)
        elif state == "rating":
            self._on_rating(chat_id, session, text)

    def _is_allowed(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self._allowed

    # ── session ──
    def _new_session(self) -> dict:
        return {"state": "identify", "user": None, "ticket_id": None, "name": None,
                "problems": [], "turns": [], "debug": False, "rating": None,
                "last_active": time.time()}

    def _begin(self, chat_id: str) -> None:
        self._sessions[chat_id] = self._new_session()
        self._send(chat_id, _IDENTIFY_PROMPT)

    def _end(self, chat_id: str) -> None:
        existed = self._sessions.pop(chat_id, None) is not None
        self._send(chat_id, "Session closed. Send /start to begin a new one."
                   if existed else "No active session. Send /start to begin.")

    # ── stages ──
    def _on_identify(self, chat_id: str, session: dict, text: str) -> None:
        user, ticket, guest = resolve_identity(text)
        session["user"] = user
        session["ticket_id"] = ticket["id"] if ticket else None
        if user:
            session["name"] = user.get("name")
            confirm = f"Thanks, {user['name']} — found your account ({user.get('plan', '?')} plan)."
        else:
            session["name"] = guest
            confirm = f"Thanks{', ' + guest if guest else ''}."
        session["state"] = "problem"
        self._send(chat_id, f"{confirm} What can I help you with? Describe the problem.")

    def _process_problem(self, chat_id: str, session: dict, text: str) -> None:
        session["turns"].append({"role": "user", "content": text})
        uid = (session["user"] or {}).get("id")
        try:
            result = support_service.answer(
                text, session.get("ticket_id"), uid,
                style="debug" if session["debug"] else "plain",
                history=session["turns"][:-1], min_score=self._min_score,
            )
        except Exception as exc:  # noqa: BLE001 — report, keep the session alive
            self._send(chat_id, f"Sorry, I hit an error answering that: {exc}")
            return

        if result.get("answered"):
            body = result["answer"]
            if session["debug"]:
                tk = (result.get("ticket") or {}).get("id") or session.get("ticket_id") or "none"
                srcs = ", ".join(result.get("sources") or []) or "none"
                body = f"🔎 user: {session.get('name') or 'unknown'}; ticket: {tk}; sources: {srcs}\n\n{body}"
            session["problems"].append({"question": text, "answered": True})
            session["turns"].append({"role": "assistant", "content": result["answer"]})
        else:
            ref = f"REQ-{1001 + sum(1 for p in session['problems'] if not p['answered'])}"
            session["problems"].append({"question": text, "answered": False, "ref": ref})
            session["turns"].append({"role": "assistant",
                                     "content": f"(logged for developers as {ref})"})
            body = ("I don't have an answer for that in our documentation yet. I've logged "
                    f"your request ({ref}) and our developers will follow up.")

        session["state"] = "another"
        self._send(chat_id, f"{body}\n\n{_ANOTHER_PROMPT}")

    def _on_another(self, chat_id: str, session: dict, text: str) -> None:
        if text.strip().lower().rstrip(".!") in _NEGATIVE:
            session["state"] = "rating"
            self._send(chat_id, _RATING_PROMPT)
        else:
            self._process_problem(chat_id, session, text)

    def _on_rating(self, chat_id: str, session: dict, text: str) -> None:
        match = re.search(r"[1-5]", text)
        if not match:
            self._send(chat_id, "Please reply with a number from 1 to 5.")
            return
        session["rating"] = int(match.group())
        name = session.get("name")
        logged = [p for p in session["problems"] if not p["answered"]]
        solved = len(session["problems"]) - len(logged)
        parts = [f"Thank you{', ' + name if name else ''}! Your rating ({session['rating']}/5) is recorded."]
        if solved:
            parts.append(f"{solved} question(s) answered.")
        if logged:
            parts.append(f"{len(logged)} request(s) logged for the developers "
                         f"({', '.join(p['ref'] for p in logged)}).")
        parts.append("Session closed — send /start anytime. 👋")
        self._send(chat_id, " ".join(parts))
        logger.info("support session done: user=%s rating=%s solved=%d logged=%d",
                    name, session["rating"], solved, len(logged))
        self._sessions.pop(chat_id, None)

    # ── telegram helper ──
    def _send(self, chat_id: str, text: str) -> None:
        _call_telegram(self._token, "sendMessage", {"chat_id": chat_id, "text": text})
