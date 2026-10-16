"""Server-owned Web identities and durable control-plane resource ownership.

Local workspace and authenticated desktop IPC retain their single-user semantics.
Network users never obtain identity or ownership from client-supplied identifiers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import uuid
from http.cookies import CookieError, SimpleCookie
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PRINCIPAL_ATTR = "_jiuwen_web_principal"
AUTH_COOKIE = "jiuwenswarm_web_auth"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}\Z")


class WebAccessDenied(ValueError):
    pass


@dataclass(frozen=True)
class WebPrincipal:
    user_id: str
    workspace_owner: bool = False


_CURRENT: ContextVar[WebPrincipal | None] = ContextVar("web_principal", default=None)


def principal_for(ws) -> WebPrincipal | None:
    return getattr(ws, PRINCIPAL_ATTR, None)


@contextlib.contextmanager
def identity_scope(principal):
    marker = _CURRENT.set(principal)
    try:
        yield
    finally:
        _CURRENT.reset(marker)


def restricted_principal() -> WebPrincipal | None:
    current = _CURRENT.get()
    return current if current and not current.workspace_owner else None


def is_loopback(value) -> bool:
    if value == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(value)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except (TypeError, ValueError):
        return False


def request_headers(ws):
    return getattr(getattr(ws, "request", None), "headers", None) or getattr(ws, "request_headers", None)


def header(ws, name):
    headers = request_headers(ws)
    if headers is None:
        return None
    try:
        return headers.get(name) or headers.get(name.lower())
    except (ValueError, KeyError) as exc:
        raise WebAccessDenied("Ambiguous authentication headers") from exc


class WebIdentityPolicy:
    def __init__(self):
        self._credentials: list[tuple[bytes, WebPrincipal]] = []
        raw = os.environ.get("JIUWENSWARM_WEB_USERS", "").strip()
        shared = os.environ.get("JIUWENSWARM_WEB_TOKEN", "").strip()
        if raw and shared:
            raise ValueError("Configure WEB_USERS or WEB_TOKEN, not both")
        if raw:
            try:
                users = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid JIUWENSWARM_WEB_USERS JSON") from exc
            if not isinstance(users, dict) or not users:
                raise ValueError("WEB_USERS must be a nonempty user-to-token object")
            admins = {v.strip() for v in os.environ.get("JIUWENSWARM_WEB_ADMINS", "").split(",") if v.strip()}
            if not admins.issubset(users):
                raise ValueError("WEB_ADMINS must reference configured Web users")
            for user_id, token in users.items():
                self._add(user_id, token, workspace_owner=user_id in admins)
        elif shared:
            self._add(os.environ.get("JIUWENSWARM_WEB_USER_ID", "local"), shared, workspace_owner=True)

    def _add(self, user_id, token, *, workspace_owner):
        if not isinstance(user_id, str) or not _IDENTIFIER.fullmatch(user_id):
            raise ValueError("Invalid Web user identifier")
        if not isinstance(token, str) or len(token.encode()) < 32 or any(ord(c) <= 32 for c in token):
            raise ValueError("Web authentication tokens must contain at least 32 bytes without whitespace")
        encoded = token.encode()
        if any(hmac.compare_digest(encoded, old) for old, _ in self._credentials):
            raise ValueError("Web users must have distinct tokens")
        self._credentials.append((encoded, WebPrincipal(user_id, workspace_owner)))

    @property
    def configured(self):
        return bool(self._credentials)

    def validate_listener(self, host):
        if not self.configured and not is_loopback(host):
            raise WebAccessDenied("Non-loopback WebSocket listeners require Web authentication")

    def verify(self, token):
        if not isinstance(token, str) or len(token) > 4096:
            raise WebAccessDenied("Web authentication failed")
        supplied = token.encode()
        for expected, principal in self._credentials:
            if hmac.compare_digest(expected, supplied):
                return principal
        raise WebAccessDenied("Web authentication failed")

    @staticmethod
    def local_identity(peer, local, origin):
        if not (isinstance(peer, tuple) and isinstance(local, tuple)
                and is_loopback(peer[0]) and is_loopback(local[0])):
            raise WebAccessDenied("Web authentication required")
        if origin:
            try:
                parsed = urlsplit(origin)
                if parsed.scheme not in ("http", "https") or not is_loopback(parsed.hostname):
                    raise ValueError("Untrusted browser origin")
            except ValueError as exc:
                raise WebAccessDenied("Untrusted browser origin") from exc
        return WebPrincipal("local", workspace_owner=True)

    def authenticate_http(self, headers, peer, local):
        if not self.configured:
            return self.local_identity(peer, local, headers.get("Origin"))
        authorization = headers.get("Authorization", "")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer":
                raise WebAccessDenied("Web authentication failed")
        else:
            cookies = SimpleCookie()
            try:
                cookies.load(headers.get("Cookie", ""))
                token = cookies[AUTH_COOKIE].value
            except (CookieError, KeyError, ValueError) as exc:
                raise WebAccessDenied("Web authentication required") from exc
        return self.verify(token)

    async def authenticate(self, ws):
        browser_auth = False
        authorization = header(ws, "Authorization")
        if self.configured and authorization is not None:
            scheme, _, token = str(authorization).partition(" ")
            if scheme.lower() != "bearer":
                raise WebAccessDenied("Web authentication failed")
            principal = self.verify(token)
        elif self.configured:
            browser_auth = True
            # Browser WebSocket API cannot set Authorization. No business frame
            # or connection hook runs until this credential-only frame is checked.
            try:
                await ws.send(json.dumps({"type": "event", "event": "auth.required", "payload": {}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                if len(raw) > 8192:
                    raise ValueError("Authentication frame too large")
                first = json.loads(raw)
                if not isinstance(first, dict) or first.get("type") != "auth":
                    raise ValueError("Authentication frame required")
                principal = self.verify(first.get("token"))
            except (ValueError, TypeError, asyncio.TimeoutError) as exc:
                raise WebAccessDenied("Web authentication required") from exc
        else:
            peer = getattr(ws, "remote_address", None)
            local = getattr(ws, "local_address", None)
            if header(ws, "X-Jiuwen-Remote-Proxy"):
                raise WebAccessDenied("Web authentication required")
            principal = self.local_identity(peer, local, header(ws, "Origin"))
        setattr(ws, PRINCIPAL_ATTR, principal)
        if browser_auth:
            await ws.send(json.dumps({"type": "event", "event": "auth.ok",
                                      "payload": {"user_id": principal.user_id}}))
        return principal


def web_client_headers(url: str) -> dict[str, str]:
    """Credentials for native clients, never inferred from another user's token.

    A deployment's single-user secret is inherited only for its configured local
    ports. Remote/custom client destinations require an explicit client token.
    """
    token = os.environ.get("JIUWENSWARM_WEB_CLIENT_TOKEN", "").strip()
    if not token:
        parsed = urlsplit(url)
        ports = {int(os.environ.get("WEB_PORT", "19000")),
                 int(os.environ.get("GATEWAY_PORT") or os.environ.get("JIUWENSWARM_GATEWAY_PORT", "19001"))}
        if is_loopback(parsed.hostname) and parsed.port in ports:
            token = os.environ.get("JIUWENSWARM_WEB_TOKEN", "").strip()
    return {"Authorization": "Bearer " + token} if token else {}


def _owner_path(kind: str, identifier: str) -> Path:
    if kind not in {"sessions", "projects"} or not _IDENTIFIER.fullmatch(identifier):
        raise WebAccessDenied("Resource unavailable")
    from jiuwenswarm.common.utils import get_agent_root_dir
    digest = hashlib.sha256(identifier.encode()).hexdigest()
    return get_agent_root_dir() / ".web-owners" / kind / digest / "owner.json"


def owner_of(kind: str, identifier: str) -> str | None:
    path = _owner_path(kind, identifier)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["user_id"] if isinstance(data, dict) and data.get("id") == identifier else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def claim_resource(kind: str, identifier: str, principal: WebPrincipal) -> None:
    """Exclusive creation; neither legacy metadata nor a second user can claim it."""
    path = _owner_path(kind, identifier)
    path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        if owner_of(kind, identifier) != principal.user_id:
            raise WebAccessDenied("Resource unavailable") from None
        return
    with path.open("x", encoding="utf-8") as output:
        json.dump({"id": identifier, "user_id": principal.user_id}, output)
        output.flush()
        os.fsync(output.fileno())


def can_access(principal: WebPrincipal | None, kind: str, identifier: str) -> bool:
    if principal is None:
        return False
    if principal.workspace_owner:
        return True
    try:
        return bool(identifier and owner_of(kind, identifier) == principal.user_id)
    except (WebAccessDenied, TypeError):
        return False


def visible_resource(kind: str, identifier: str) -> bool:
    principal = restricted_principal()
    return principal is None or can_access(principal, kind, identifier)


_SESSION_METHODS = frozenset({
    "session.get_metadata", "session.rename", "session.pin", "session.delete", "session.switch",
    "session.rewind", "session.rewind_and_restore", "session.get_status", "history.get",
    "chat.send", "chat.resume", "chat.interrupt", "chat.user_answer", "command.goal",
})
_PROJECT_METHODS = frozenset({
    "project.info", "project.get_sessions", "project.get_cron_sessions", "project.rename",
    "project.pin", "project.remove", "project.restore", "project.git.status", "project.git.probe",
    "project.git.diff_status", "project.git.turn_diff_list", "project.git.turn_diff",
    "project.git.diff_watch", "project.git.diff_files_watch", "project.git.diff_detail_watch",
    "project.git.diff_unwatch", "project.git.discard_turn_changes", "project.git.redo_turn_changes",
    "project.git.init", "project.git.switch_branch", "project.git.create_branch", "project.git.commit",
    "project.git.push",
})
_LIST_METHODS = frozenset({"session.list", "project.list", "project.pinned_sessions"})
_DEFAULT_PROJECTS = frozenset({"default", "default_code", "default_design"})


def authorize_rpc(principal: WebPrincipal, method: str, params: dict) -> None:
    if principal.workspace_owner:
        return
    if method not in _SESSION_METHODS | _PROJECT_METHODS | _LIST_METHODS | {"session.create", "project.create"}:
        raise WebAccessDenied("Workspace administrator required")
    if method == "project.create":
        # An owned project cannot alias another user's history or workspace.
        from jiuwenswarm.common.utils import get_agent_root_dir
        workspace_base = (get_agent_root_dir() / "web-workspaces").resolve()
        assigned_root = workspace_base / hashlib.sha256(principal.user_id.encode()).hexdigest()
        user_root = assigned_root.resolve()
        if user_root != assigned_root:
            raise WebAccessDenied("User workspace cannot be redirected")
        supplied = params.get("project_dir")
        directory = Path(supplied).resolve() if isinstance(supplied, str) and supplied else user_root / uuid.uuid4().hex
        if directory == user_root or not directory.is_relative_to(user_root):
            raise WebAccessDenied("New projects require a user workspace directory")
        params["project_dir"] = str(directory)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"session_id", "source_session_id", "target_session_id", "parent_session_id", "conversation_id"}:
                    if item and (not isinstance(item, str) or not can_access(principal, "sessions", item)):
                        raise WebAccessDenied("Session unavailable")
                elif key == "session_ids":
                    if not isinstance(item, list) or any(not isinstance(sid, str)
                            or not can_access(principal, "sessions", sid) for sid in item):
                        raise WebAccessDenied("Session unavailable")
                elif key == "project_id" and item:
                    if not isinstance(item, str) or (item not in _DEFAULT_PROJECTS
                            and not can_access(principal, "projects", item)):
                        raise WebAccessDenied("Project unavailable")
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(params)
    if method in _SESSION_METHODS and not params.get("session_id"):
        raise WebAccessDenied("Session required")
    if method in _PROJECT_METHODS and method != "project.git.diff_unwatch" and not params.get("project_id"):
        raise WebAccessDenied("Project required")
    if method.startswith("project.git.") and params.get("project_id") in _DEFAULT_PROJECTS:
        raise WebAccessDenied("An owned project is required for Git operations")
