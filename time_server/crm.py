"""
Mock CRM data access — the "connect your CRM" side of the support assistant.

Stands in for a real CRM / helpdesk API: it loads users and tickets from a JSON file
and offers a few lookups. It is deliberately a thin module so it can back both the
MCP tools (``jarvis.get_ticket`` etc. in server.py) and the ``/support`` endpoint
without duplication. Swapping the JSON for a real CRM later means reimplementing
these functions against that API — the callers don't change.

Config (env):
    JARVIS_CRM_PATH   path to the CRM JSON (default: the bundled support_data/crm.json)
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent / "support_data" / "crm.json"


def _crm_path() -> Path:
    override = os.environ.get("JARVIS_CRM_PATH", "").strip()
    return Path(override) if override else _DEFAULT_PATH


@lru_cache(maxsize=8)
def _load(path_str: str) -> dict:
    """Load and cache the CRM JSON (keyed by path so tests can point elsewhere)."""
    with open(path_str, encoding="utf-8") as handle:
        data = json.load(handle)
    return {
        "users": {u["id"]: u for u in data.get("users", [])},
        "tickets": {t["id"]: t for t in data.get("tickets", [])},
    }


def _db() -> dict:
    return _load(str(_crm_path()))


def get_ticket(ticket_id: str) -> dict | None:
    """Return the ticket with ``ticket_id``, or None if there is no such ticket."""
    return _db()["tickets"].get((ticket_id or "").strip())


def get_user(user_id: str) -> dict | None:
    """Return the user with ``user_id``, or None if there is no such user."""
    return _db()["users"].get((user_id or "").strip())


def list_user_tickets(user_id: str) -> list[dict]:
    """Return all tickets belonging to ``user_id`` (newest first), or an empty list."""
    tickets = [t for t in _db()["tickets"].values() if t.get("user_id") == (user_id or "").strip()]
    return sorted(tickets, key=lambda t: t.get("created", ""), reverse=True)


def find_user_by_email(email: str) -> dict | None:
    """Return the user whose email matches ``email`` (case-insensitive), or None."""
    target = (email or "").strip().lower()
    if not target:
        return None
    for user in _db()["users"].values():
        if (user.get("email") or "").strip().lower() == target:
            return user
    return None


def find_tickets(product_area: str | None = None, status: str | None = None,
                 user_id: str | None = None) -> list[dict]:
    """Return tickets matching the given filters (newest first).

    Used by the support bot's symptom→ticket matching (e.g. all open ``auth`` tickets,
    optionally scoped to a known user).
    """
    out = []
    for ticket in _db()["tickets"].values():
        if product_area and ticket.get("product_area") != product_area:
            continue
        if status and ticket.get("status") != status:
            continue
        if user_id and ticket.get("user_id") != user_id:
            continue
        out.append(ticket)
    return sorted(out, key=lambda t: t.get("created", ""), reverse=True)


def clear_cache() -> None:
    """Drop the load cache (tests that switch JARVIS_CRM_PATH between files)."""
    _load.cache_clear()
