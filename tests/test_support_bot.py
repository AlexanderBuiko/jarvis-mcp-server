"""Tests for the staged Telegram support bot — Telegram + LLM brain are faked."""

import pytest

from time_server import support_bot, support_service


@pytest.fixture
def bot(monkeypatch):
    sent: list[str] = []

    def fake_call(token, method, params, timeout=30):
        if method == "sendMessage":
            sent.append(params["text"])
        return {}

    def fake_answer(question, ticket_id=None, user_id=None, style="plain",
                    history=None, min_score=None):
        answered = "zzz" not in question.lower()   # 'zzz' → simulate "not in the docs"
        return {
            "answered": answered,
            "answer": f"ANSWER(tid={ticket_id},uid={user_id},style={style})" if answered else "",
            "sources": ["authorization.md"] if answered else [],
            "ticket": {"id": ticket_id} if ticket_id else None,
            "user": {"id": user_id} if user_id else None,
        }

    monkeypatch.setattr(support_bot, "_call_telegram", fake_call)
    monkeypatch.setattr(support_service, "answer", fake_answer)
    b = support_bot.SupportBot(token="tkn", idle_s=1000, min_score=0.25)
    b._allowed = {"42"}
    return b, sent


def _msg(text, user="42", chat="99"):
    return {"message": {"from": {"id": user}, "chat": {"id": chat}, "text": text}}


def test_rejects_strangers(bot):
    b, sent = bot
    b.handle_update(_msg("/start", user="999"))
    assert sent == []


def test_no_response_before_start(bot):
    b, sent = bot
    b.handle_update(_msg("hello, I have a problem"))
    assert sent and "start" in sent[-1].lower()
    assert "99" not in b._sessions               # no session created


def test_start_asks_identity(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    assert b._sessions["99"]["state"] == "identify"
    assert "who am i helping" in sent[-1].lower()


def test_identify_by_user_id(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("I'm U-1"))
    s = b._sessions["99"]
    assert s["user"]["id"] == "U-1"
    assert s["state"] == "problem"
    assert "Dana" in sent[-1]                     # greeted by the real account name


def test_identity_is_not_guessed_from_symptom(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("401 unauthorized error"))   # a symptom, NOT an identity
    s = b._sessions["99"]
    assert s["user"] is None                      # must NOT resolve to Dana
    assert "Dana" not in sent[-1]
    assert s["state"] == "problem"


def test_answered_problem_then_asks_another(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("U-1"))
    b.handle_update(_msg("why is authorization failing?"))
    s = b._sessions["99"]
    assert s["state"] == "another"
    assert s["problems"][-1]["answered"] is True
    assert "anything else" in sent[-1].lower()


def test_unknown_problem_is_logged_for_devs(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("Alex"))
    b.handle_update(_msg("zzz my espresso machine is broken"))
    s = b._sessions["99"]
    assert s["problems"][-1]["answered"] is False
    assert "ref" in s["problems"][-1]
    assert "developers will follow up" in sent[-1].lower()


def test_multiple_problems_loop(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("U-1"))
    b.handle_update(_msg("first problem"))     # answered → another
    b.handle_update(_msg("second problem"))    # another(non-negative) → processed → another
    s = b._sessions["99"]
    assert len(s["problems"]) == 2
    assert s["state"] == "another"


def test_no_more_problems_goes_to_rating(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("U-1"))
    b.handle_update(_msg("why is auth failing?"))
    b.handle_update(_msg("no"))
    assert b._sessions["99"]["state"] == "rating"
    assert "1" in sent[-1] and "5" in sent[-1]


def test_rating_thanks_and_closes(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("U-1"))
    b.handle_update(_msg("why is auth failing?"))
    b.handle_update(_msg("no"))
    b.handle_update(_msg("5 - great"))
    assert "99" not in b._sessions               # session closed
    assert "thank you" in sent[-1].lower()
    assert "5/5" in sent[-1]


def test_rating_requires_a_number(bot):
    b, sent = bot
    b.handle_update(_msg("/start"))
    b.handle_update(_msg("U-1"))
    b.handle_update(_msg("why is auth failing?"))
    b.handle_update(_msg("no"))
    b.handle_update(_msg("it was fine"))         # no digit
    assert b._sessions["99"]["state"] == "rating"
    assert "1 to 5" in sent[-1]
