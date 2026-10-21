# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async HTTP client for the relay's group chat API.

Only the two calls the project page needs are implemented — create a group and
list the caller's groups. The full surface (invites, members, messages,
subscribe) is deliberately left out until something asks for it.

Contract notes that shaped this file, all verified against the relay's
``a2x_registry/groupchat/router.py``:

* Every route requires ``Authorization: Bearer a2x_pat_…``. There is no
  anonymous mode, so an empty ``api_key`` is a configuration error, not a
  degraded mode.
* The relay reports errors in **two** shapes. Business rejections come back as
  ``{"error": {"code", "message"}}``; the disabled-module and auth-dependency
  paths use FastAPI's ``{"detail": ...}``, where ``detail`` is a *string* for
  auth failures but a *dict* when the module is off. ``_raise_for_status``
  handles all three.
* ``join_policy`` and ``max_members`` are not validated server-side: a value
  outside the enum reaches ``Group.__post_init__`` and a non-integer reaches
  ``int()``, both surfacing as unhandled 500s. They are validated here instead.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Literal
from urllib.parse import quote

import httpx

from .config import (
    JOIN_POLICIES,
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    ROLES,
    GroupChatConfig,
)
from .errors import (
    GroupChatError,
    GroupChatNotConfiguredError,
    GroupChatRequestInvalidError,
    GroupChatUnavailableError,
)

HTTPMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]

GROUPS_PATH = "api/groups"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/"


def _encode(segment: str) -> str:
    """Percent-encode one path segment (principal ids and tokens are opaque)."""
    return quote(str(segment), safe="")


def _group_path(group_id: Any) -> str:
    return f"{GROUPS_PATH}/{_coerce_positive_int(group_id, 'group_id')}"


def _coerce_positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise GroupChatRequestInvalidError(f"{field} 必须是正整数") from exc
    if parsed < 1:
        raise GroupChatRequestInvalidError(f"{field} 必须大于 0")
    return parsed


def _coerce_non_negative_int(value: Any, field: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise GroupChatRequestInvalidError(f"{field} 必须是非负整数") from exc
    if parsed < 0:
        raise GroupChatRequestInvalidError(f"{field} 不能为负")
    return parsed


def _build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def validate_principal_id(principal_id: Any) -> str:
    """Validate a member/invitee principal id.

    The relay treats this as an opaque string, but an empty one produces a
    confusing 404/409 rather than a clear rejection, so it is checked here.
    """
    cleaned = str(principal_id or "").strip()
    if not cleaned:
        raise GroupChatRequestInvalidError("成员标识不能为空")
    if len(cleaned) > 128:
        raise GroupChatRequestInvalidError("成员标识过长")
    return cleaned


def validate_role(role: Any) -> str:
    cleaned = str(role or "").strip()
    if cleaned not in ROLES:
        raise GroupChatRequestInvalidError(f"角色必须是 {'、'.join(ROLES)} 之一")
    if cleaned == "owner":
        # The relay rejects ownership transfer outright (groupchat_role_invalid).
        raise GroupChatRequestInvalidError("不能通过角色变更转移群主")
    return cleaned


def validate_max_uses(value: Any) -> int:
    if isinstance(value, bool):
        raise GroupChatRequestInvalidError("使用次数必须是正整数")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise GroupChatRequestInvalidError("使用次数必须是正整数") from exc
    if parsed < 1:
        raise GroupChatRequestInvalidError("使用次数必须大于 0")
    return parsed


def validate_ttl_seconds(value: Any) -> int:
    if isinstance(value, bool):
        raise GroupChatRequestInvalidError("有效期必须是整数秒")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise GroupChatRequestInvalidError("有效期必须是整数秒") from exc
    if parsed < 0:
        raise GroupChatRequestInvalidError("有效期不能为负")
    return parsed


def validate_message_text(text: Any) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        raise GroupChatRequestInvalidError("消息内容不能为空")
    if len(cleaned) > MAX_MESSAGE_CHARS:
        raise GroupChatRequestInvalidError(
            f"消息内容不能超过 {MAX_MESSAGE_CHARS} 个字符"
        )
    return cleaned


def _clean_mentions(mentions: Any) -> list[str]:
    if mentions is None:
        return []
    if not isinstance(mentions, (list, tuple, set)):
        raise GroupChatRequestInvalidError("mentions 必须是列表")
    out: list[str] = []
    for item in mentions:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _extract_error(resp: httpx.Response) -> tuple[str, str]:
    """Return ``(code, message)`` from either error envelope.

    Falls back to a synthesized code when the body is unparseable or empty, so
    callers always get something branchable.
    """
    try:
        payload = resp.json()
    except ValueError:
        return f"http_{resp.status_code}", resp.reason_phrase or "request failed"
    if not isinstance(payload, dict):
        return f"http_{resp.status_code}", resp.reason_phrase or "request failed"

    # Business errors: {"error": {"code", "message"}}
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or f"http_{resp.status_code}")
        message = str(error.get("message") or error.get("code") or "request failed")
        return code, message
    if isinstance(error, str):
        return str(payload.get("code") or f"http_{resp.status_code}"), error

    # Dependency errors: {"detail": {...}} for the disabled module,
    # {"detail": "..."} for auth. The nested form carries the useful code.
    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = str(detail.get("code") or f"http_{resp.status_code}")
        message = str(detail.get("message") or detail.get("code") or "request failed")
        return code, message
    if isinstance(detail, str) and detail:
        return f"http_{resp.status_code}", detail

    return f"http_{resp.status_code}", resp.reason_phrase or "request failed"


def validate_group_name(name: Any) -> str:
    """Validate and normalize a group name; raise if the relay would reject it."""
    cleaned = str(name or "").strip()
    if not cleaned:
        raise GroupChatRequestInvalidError("项目名称不能为空")
    if len(cleaned) > MAX_NAME_CHARS:
        raise GroupChatRequestInvalidError(f"项目名称不能超过 {MAX_NAME_CHARS} 个字符")
    # Mirrors the relay's rejection of Cc/Cf/Cs categories (it would 400, but
    # catching it here gives a specific message instead of a generic one).
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in cleaned):
        raise GroupChatRequestInvalidError("项目名称不能包含控制字符")
    return cleaned


def validate_join_policy(join_policy: Any) -> str:
    """Validate ``join_policy``; the relay would 500 on an unknown value."""
    cleaned = str(join_policy or "invite_only").strip()
    if cleaned not in JOIN_POLICIES:
        allowed = "、".join(JOIN_POLICIES)
        raise GroupChatRequestInvalidError(f"join_policy 必须是 {allowed} 之一")
    return cleaned


def validate_max_members(max_members: Any, *, ceiling: int) -> int:
    """Validate ``max_members``; the relay would 500 on a non-integer."""
    if max_members is None or max_members == "":
        return ceiling
    if isinstance(max_members, bool):
        raise GroupChatRequestInvalidError("成员上限必须是正整数")
    try:
        value = int(max_members)
    except (TypeError, ValueError) as exc:
        raise GroupChatRequestInvalidError("成员上限必须是正整数") from exc
    if value < 1:
        raise GroupChatRequestInvalidError("成员上限必须大于 0")
    if value > ceiling:
        raise GroupChatRequestInvalidError(f"成员上限不能超过 {ceiling}")
    return value


class AsyncGroupChatClient:
    """Minimal async client for the relay group chat API."""

    def __init__(self, config: GroupChatConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def config(self) -> GroupChatConfig:
        return self._config

    def _get_client(self) -> httpx.AsyncClient:
        if not self._config.configured:
            raise GroupChatNotConfiguredError(
                "群聊服务未配置（groupchat.enabled / groupchat.base_url）"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_normalize_base_url(self._config.base_url),
                timeout=self._config.timeout,
                headers=_build_headers(self._config.api_key),
            )
        return self._client

    async def _request(
        self,
        method: HTTPMethod,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = self._get_client()
        try:
            resp = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise GroupChatUnavailableError(
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            code, message = _extract_error(resp)
            raise GroupChatError(
                message, code=code, status_code=resp.status_code
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise GroupChatUnavailableError(
                "群聊服务返回了非 JSON 响应", status_code=resp.status_code
            ) from exc

    async def create_group(
        self,
        *,
        name: str,
        owner_dataset: str | None = None,
        join_policy: str = "invite_only",
        max_members: Any = None,
    ) -> dict[str, Any]:
        """Create a group. Returns ``{group_id, name, owner_dataset, seq_counter}``.

        The relay assigns the owner from the authenticated principal, so no
        owner field is sent. There is no idempotency key: a retry creates a
        second group, which is why the UI must disable its submit button while
        this is in flight.
        """
        cleaned_name = validate_group_name(name)
        cleaned_policy = validate_join_policy(join_policy)
        cleaned_max = validate_max_members(
            max_members, ceiling=self._config.default_max_members
        )

        body: dict[str, Any] = {
            "name": cleaned_name,
            "join_policy": cleaned_policy,
            "max_members": cleaned_max,
        }
        if owner_dataset:
            body["owner_dataset"] = str(owner_dataset).strip()

        result = await self._request("POST", GROUPS_PATH, json=body)
        return result if isinstance(result, dict) else {}

    async def list_groups(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List groups the configured principal is an active member of."""
        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            safe_limit = 50

        result = await self._request("GET", GROUPS_PATH, params={"limit": safe_limit})
        if not isinstance(result, dict):
            return []
        groups = result.get("groups")
        return [item for item in groups if isinstance(item, dict)] if isinstance(groups, list) else []

    async def get_group(self, *, group_id: int) -> dict[str, Any]:
        """Group detail plus its visible member list."""
        result = await self._request("GET", _group_path(group_id))
        return result if isinstance(result, dict) else {}

    async def whoami(self) -> dict[str, Any]:
        """Return the identity the configured API key authenticates as.

        The UI needs this to tell its own messages from everyone else's: the
        credential is the only thing that identifies the local side, since the
        relay stamps every message with the *sender's* principal id.
        """
        result = await self._request("GET", "api/auth/whoami")
        return result if isinstance(result, dict) else {}

    async def delete_group(self, *, group_id: int) -> dict[str, Any]:
        """Delete a group and everything it owns. Irreversible, owner only.

        The relay drops the group's members, invitations and messages in the
        same operation and broadcasts a ``group_deleted`` frame to any live
        subscribers before the rows go.
        """
        result = await self._request("DELETE", _group_path(group_id))
        return result if isinstance(result, dict) else {}

    async def set_archived(self, *, group_id: int, archived: bool) -> dict[str, Any]:
        """Archive or unarchive a group.

        Archiving is the reversible counterpart to deletion: the rows stay and
        the history stays readable, but the group drops out of every listing.
        The relay requires owner/admin for this.
        """
        result = await self._request(
            "PATCH", _group_path(group_id), json={"archived": bool(archived)}
        )
        return result if isinstance(result, dict) else {}

    async def leave_group(self, *, group_id: int) -> dict[str, Any]:
        """Leave a group as the current principal."""
        result = await self._request("DELETE", f"{_group_path(group_id)}/members/me")
        return result if isinstance(result, dict) else {}

    # ── Members ──────────────────────────────────────────────────────────────

    async def list_members(self, *, group_id: int) -> list[dict[str, Any]]:
        """Active members of a group.

        The relay filters this endpoint to ``status == "active"`` — pending
        invites are only visible to admins, and only through ``get_group``.
        """
        result = await self._request("GET", f"{_group_path(group_id)}/members")
        if not isinstance(result, dict):
            return []
        members = result.get("members")
        return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []

    async def remove_member(self, *, group_id: int, principal_id: str) -> dict[str, Any]:
        """Kick a member (requires owner/admin on the relay side)."""
        target = validate_principal_id(principal_id)
        result = await self._request(
            "DELETE", f"{_group_path(group_id)}/members/{_encode(target)}"
        )
        return result if isinstance(result, dict) else {}

    async def set_member_role(
        self, *, group_id: int, principal_id: str, role: str
    ) -> dict[str, Any]:
        """Change a member's role. ``owner`` is not grantable — the relay rejects it."""
        target = validate_principal_id(principal_id)
        cleaned_role = validate_role(role)
        result = await self._request(
            "PUT",
            f"{_group_path(group_id)}/members/{_encode(target)}/role",
            json={"role": cleaned_role},
        )
        return result if isinstance(result, dict) else {}

    # ── Invitations ──────────────────────────────────────────────────────────

    async def create_invite(
        self,
        *,
        group_id: int,
        invitee_id: str | None = None,
        max_uses: Any = None,
        ttl_seconds: Any = None,
    ) -> dict[str, Any]:
        """Create a targeted (``invitee_id``) or open invitation.

        Returns ``{invite_id, group_id, mode, invitee_id, token, expires_at, max_uses}``.
        The plaintext ``token`` appears **only** in this response — the relay
        stores just its hash — so callers must surface it immediately or lose it.
        """
        body: dict[str, Any] = {}
        if invitee_id is not None and str(invitee_id).strip():
            body["invitee_id"] = validate_principal_id(invitee_id)
        if max_uses is not None and str(max_uses).strip() != "":
            body["max_uses"] = validate_max_uses(max_uses)
        if ttl_seconds is not None and str(ttl_seconds).strip() != "":
            body["ttl_seconds"] = validate_ttl_seconds(ttl_seconds)

        result = await self._request(
            "POST", f"{_group_path(group_id)}/invites", json=body
        )
        return result if isinstance(result, dict) else {}

    async def accept_invite(self, *, token: str) -> dict[str, Any]:
        """Redeem an invitation token; makes the caller an active member."""
        cleaned = str(token or "").strip()
        if not cleaned:
            raise GroupChatRequestInvalidError("邀请 token 不能为空")
        result = await self._request("POST", f"api/invites/{_encode(cleaned)}/accept")
        return result if isinstance(result, dict) else {}

    # ── Messages ─────────────────────────────────────────────────────────────

    async def send_message(
        self,
        *,
        group_id: int,
        text: str,
        client_msg_id: str | None = None,
        mentions: Any = None,
        reply_to: Any = None,
    ) -> dict[str, Any]:
        """Post a text message.

        ``client_msg_id`` is the relay's idempotency key, scoped to
        ``(group_id, sender)`` — a retry with the same value returns the
        original entry rather than duplicating it.
        """
        cleaned_text = validate_message_text(text)
        body: dict[str, Any] = {"payload": cleaned_text, "kind": "text"}
        if client_msg_id:
            body["client_msg_id"] = str(client_msg_id)[:128]
        cleaned_mentions = _clean_mentions(mentions)
        if cleaned_mentions:
            body["mentions"] = cleaned_mentions
        if reply_to is not None and str(reply_to).strip() != "":
            try:
                body["reply_to"] = int(reply_to)
            except (TypeError, ValueError) as exc:
                raise GroupChatRequestInvalidError("reply_to 必须是消息序号") from exc

        result = await self._request(
            "POST", f"{_group_path(group_id)}/messages", json=body
        )
        return result if isinstance(result, dict) else {}

    async def fetch_messages(
        self,
        *,
        group_id: int,
        after_seq: Any = None,
        before_seq: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Pull a page of the log.

        Omitting ``after_seq`` makes the relay use its own recorded cursor,
        which is what lets a client resume after a restart without replaying
        the whole history.
        """
        params: dict[str, Any] = {}
        if after_seq is not None and str(after_seq).strip() != "":
            params["after_seq"] = _coerce_non_negative_int(after_seq, "after_seq")
        if before_seq is not None and str(before_seq).strip() != "":
            params["before_seq"] = _coerce_non_negative_int(before_seq, "before_seq")
        if params.get("after_seq") is not None and params.get("before_seq") is not None:
            # The relay answers 400 groupchat_cursor_conflict; catching it here
            # keeps the failure local and specific.
            raise GroupChatRequestInvalidError("after_seq 与 before_seq 不能同时指定")
        if limit is not None and str(limit).strip() != "":
            params["limit"] = _coerce_positive_int(limit, "limit")

        result = await self._request(
            "GET", f"{_group_path(group_id)}/messages", params=params
        )
        return result if isinstance(result, dict) else {}

    async def ack(self, *, group_id: int, seq: Any) -> dict[str, Any]:
        """Advance the server-side read cursor. It only ever moves forward."""
        safe_seq = _coerce_non_negative_int(seq, "seq")
        result = await self._request(
            "POST", f"{_group_path(group_id)}/messages/{safe_seq}/ack"
        )
        return result if isinstance(result, dict) else {}

    # ── Subscription ─────────────────────────────────────────────────────────

    def subscribe_url(self, *, group_id: int, after_seq: int = 0) -> tuple[str, list[str]]:
        """Build the relay WebSocket URL and the subprotocol list for ``websockets``.

        Returns ``(url, subprotocols)``. The credential goes in the handshake
        headers (not ``?token=``) so it never lands in a server access log.
        """
        config = self._config
        if not config.configured:
            raise GroupChatNotConfiguredError(
                "群聊服务未配置（groupchat.enabled / groupchat.base_url）"
            )
        base = _normalize_base_url(config.base_url)
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        url = f"{ws_base}api/groups/{int(group_id)}/subscribe?after_seq={int(after_seq)}"
        return url, []

    def subscribe_headers(self) -> dict[str, str]:
        """Bearer header for the WebSocket handshake."""
        return _build_headers(self._config.api_key)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "AsyncGroupChatClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()
