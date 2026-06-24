"""Unit tests for the pure-ASGI API-key middleware."""

import asyncio

from time_server.auth import ApiKeyAuthMiddleware


class _SpyApp:
    """Inner ASGI app that records that it was reached."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run(mw, scope):
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    return sent


def _http_scope(path="/mcp", headers=None):
    return {"type": "http", "path": path, "headers": headers or []}


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_valid_key_passes_through():
    app = _SpyApp()
    mw = ApiKeyAuthMiddleware(app, "secret")
    sent = _run(mw, _http_scope(headers=[(b"x-api-key", b"secret")]))
    assert app.called
    assert _status(sent) == 200


def test_missing_key_is_401():
    app = _SpyApp()
    mw = ApiKeyAuthMiddleware(app, "secret")
    sent = _run(mw, _http_scope(headers=[]))
    assert not app.called
    assert _status(sent) == 401


def test_wrong_key_is_401():
    app = _SpyApp()
    mw = ApiKeyAuthMiddleware(app, "secret")
    sent = _run(mw, _http_scope(headers=[(b"x-api-key", b"nope")]))
    assert not app.called
    assert _status(sent) == 401


def test_healthz_is_exempt():
    app = _SpyApp()
    mw = ApiKeyAuthMiddleware(app, "secret")
    sent = _run(mw, _http_scope(path="/healthz", headers=[]))
    assert app.called
    assert _status(sent) == 200


def test_non_http_scope_passes_through():
    app = _SpyApp()
    mw = ApiKeyAuthMiddleware(app, "secret")
    # lifespan scope must reach the inner app untouched (so startup/shutdown work).
    _run(mw, {"type": "lifespan", "path": "", "headers": []})
    assert app.called


def test_empty_key_config_is_rejected():
    try:
        ApiKeyAuthMiddleware(_SpyApp(), "")
    except ValueError:
        return
    raise AssertionError("empty api_key should raise ValueError")
