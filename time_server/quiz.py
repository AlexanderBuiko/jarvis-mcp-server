"""
Quiz pool storage, validation, and selection.

The server holds a pool of pre-generated multiple-choice questions (uploaded from
the jarvis-cli question factory) and serves a small round of them through a private
Telegram bot. It never holds the knowledge base — only the generated, transformative
MCQs.

Pool item schema (must match the generator's output):

    { "id": str, "topic": str, "question": str,
      "options": [str, str, str, str], "correct_index": int (0..3) }
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

QUESTIONS_PER_ROUND = int(os.environ.get("QUIZ_QUESTIONS_PER_ROUND", "5"))


def pool_path() -> Path:
    return Path(os.environ.get("QUIZ_POOL_PATH") or (Path.home() / ".jarvis" / "quiz_pool.json"))


def validate_pool(data) -> list[str]:
    """Return a list of schema problems; empty means the payload is a valid pool."""
    errors: list[str] = []
    if not isinstance(data, list) or not data:
        return ["pool must be a non-empty list"]
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"item {i}: not an object")
            continue
        q, opts, ci = item.get("question"), item.get("options"), item.get("correct_index")
        if not isinstance(q, str) or not q.strip():
            errors.append(f"item {i}: bad 'question'")
        if not isinstance(opts, list) or len(opts) != 4 or \
                any(not isinstance(o, str) or not o.strip() for o in opts):
            errors.append(f"item {i}: 'options' must be 4 non-empty strings")
        elif len({o.strip().lower() for o in opts}) != 4:
            errors.append(f"item {i}: options must be distinct")
        if isinstance(ci, bool) or not isinstance(ci, int) or not (0 <= (ci if isinstance(ci, int) else -1) < 4):
            errors.append(f"item {i}: 'correct_index' must be an int 0..3")
        for field in ("id", "topic"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"item {i}: missing '{field}'")
    return errors


def save_pool(data: list) -> None:
    path = pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pool() -> list:
    path = pool_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def select_questions(pool: list, n: int = QUESTIONS_PER_ROUND, seed: int | None = None) -> list:
    """Pick up to ``n`` questions from the pool.

    ``seed=None`` (the default, used by the bot on each /start) draws a fresh random
    round every time. An explicit ``seed`` makes selection deterministic — handy for
    tests and reproducible runs.
    """
    if not pool:
        return []
    rng = random.Random(seed)
    n = min(n, len(pool))
    return rng.sample(pool, n)
