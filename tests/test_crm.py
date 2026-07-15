"""Tests for the mock CRM module and its MCP tool wrappers."""

import json

from time_server import crm
from time_server import server as srv


def test_get_ticket_and_its_user():
    crm.clear_cache()
    ticket = crm.get_ticket("T-1002")
    assert ticket and ticket["subject"] == "Authorization not working"
    user = crm.get_user(ticket["user_id"])
    assert user and user["plan"] == "pro"


def test_unknown_ids_return_none():
    assert crm.get_ticket("T-0000") is None
    assert crm.get_user("U-0") is None


def test_list_user_tickets():
    ids = [t["id"] for t in crm.list_user_tickets("U-2")]
    assert "T-1003" in ids
    assert crm.list_user_tickets("U-nobody") == []


def test_custom_crm_path(tmp_path, monkeypatch):
    path = tmp_path / "crm.json"
    path.write_text(json.dumps({
        "users": [{"id": "X", "name": "Test"}],
        "tickets": [{"id": "K-1", "user_id": "X", "subject": "hi"}],
    }))
    monkeypatch.setenv("JARVIS_CRM_PATH", str(path))
    crm.clear_cache()
    try:
        assert crm.get_ticket("K-1")["user_id"] == "X"
        assert crm.get_user("X")["name"] == "Test"
    finally:
        crm.clear_cache()  # don't leak the custom path into other tests


def test_find_user_by_email():
    crm.clear_cache()
    user = crm.find_user_by_email("Dana@Example.com")   # case-insensitive
    assert user and user["id"] == "U-1"
    assert crm.find_user_by_email("nobody@example.com") is None


def test_find_tickets_filters():
    crm.clear_cache()
    auth = crm.find_tickets(product_area="auth")
    assert [t["id"] for t in auth] == ["T-1002"]
    assert crm.find_tickets(product_area="auth", user_id="U-2") == []   # not U-2's


def test_mcp_tool_wrappers():
    crm.clear_cache()
    assert json.loads(srv.get_ticket("T-1002"))["subject"] == "Authorization not working"
    assert json.loads(srv.get_ticket("T-nope")).get("error")
    assert json.loads(srv.get_user("U-1"))["name"] == "Dana Ptak"
    assert isinstance(json.loads(srv.list_user_tickets("U-2")), list)
