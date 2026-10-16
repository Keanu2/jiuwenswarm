"""Real local HTTP checks for the Web UI's adjacent workspace endpoints."""

import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.common.security.web_identity import WebPrincipal, claim_resource

TOKEN = "http-test-token-" + "x" * 32


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils
    monkeypatch.setattr(utils, "get_agent_root_dir", lambda: tmp_path)
    for name in ("JIUWENSWARM_WEB_TOKEN", "JIUWENSWARM_WEB_USERS", "JIUWENSWARM_WEB_USER_ID", "JIUWENSWARM_WEB_ADMINS"):
        monkeypatch.delenv(name, raising=False)


@contextmanager
def serving(tmp_path):
    class Handler(_SpaStaticHandler):
        project_root = workspace_root = agent_teams_root = logs_root = auto_harness_root = tmp_path

        def _build_share_snapshot(self, *, session_id):
            return {"session_id": session_id, "content": "private"}, "snapshot.json"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def request(method, path, headers=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_workspace_file_apis_require_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_WEB_TOKEN", TOKEN)
    (tmp_path / "note.md").write_text("private")
    with serving(tmp_path) as request:
        for headers in ({}, {"X-User-Id": "local"}, {"Authorization": "Bearer wrong"}):
            status, _, body = request("GET", "/file-api/raw-file?path=note.md", headers)
            assert status == 403 and b"private" not in body
        status, _, body = request("GET", "/file-api/raw-file?path=note.md",
                                  {"Authorization": "Bearer " + TOKEN})
        assert status == 200 and body == b"private"


def test_browser_cookie_exchange_and_origin_guard_preserve_file_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_WEB_TOKEN", TOKEN)
    (tmp_path / "note.md").write_text("private")
    with serving(tmp_path) as request:
        status, headers, _ = request("POST", "/web-auth/session", {"Authorization": "Bearer " + TOKEN})
        assert status == 200
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        status, _, body = request("GET", "/file-api/raw-file?path=note.md", {"Cookie": cookie})
        assert status == 200 and body == b"private"
        status, _, _ = request("GET", "/file-api/raw-file?path=note.md",
                               {"Cookie": cookie, "Origin": "https://evil.example"})
        assert status == 403
        status, _, _ = request("POST", "/web-auth/session",
                               {"Authorization": "Bearer " + TOKEN, "Origin": "https://evil.example"})
        assert status == 403


def test_multi_user_share_export_cannot_bypass_session_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_WEB_USERS", json.dumps({"alice": TOKEN, "bob": "b" * 40}))
    claim_resource("sessions", "alice-session", WebPrincipal("alice"))
    with serving(tmp_path) as request:
        for token, expected in ((TOKEN, 200), ("b" * 40, 403)):
            status, _, body = request("GET", "/share-api/snapshot?session_id=alice-session",
                                      {"Authorization": "Bearer " + token})
            assert status == expected
            assert (b"private" in body) is (expected == 200)
        status, _, _ = request("GET", "/file-api/list-files?dir=.", {"Authorization": "Bearer " + TOKEN})
        assert status == 403
