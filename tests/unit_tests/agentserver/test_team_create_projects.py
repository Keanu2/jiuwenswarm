# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the AgentServer's team.create / team.projects.list handlers.

These cover the mapping from the relay client's exceptions to the RPC error
vocabulary the web client branches on, plus the two things the projects page
depends on: creation is **not** gated on cluster mode, and a failure never
escapes the request loop.

The relay client itself is stubbed out — it is covered by
``test_groupchat_client.py`` — so these stay hermetic and fast.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.agents.harness.team.groupchat import (
    GroupChatError,
    GroupChatNotConfiguredError,
    GroupChatUnavailableError,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def fake_encode_agent_response_for_wire(resp, response_id):  # noqa: ANN001
    return {"response_id": response_id, "payload": resp.payload, "ok": resp.ok}


class Harness(agent_ws_server_module.AgentWebSocketServer):
    """Expose the group-chat handlers without going through the dispatch chain."""

    async def handle_team_create_for_test(self, ws, request, send_lock):
        await self._handle_team_create(ws, request, send_lock)

    async def handle_team_projects_list_for_test(self, ws, request, send_lock):
        await self._handle_team_projects_list(ws, request, send_lock)

    async def handle_team_project_delete_for_test(self, ws, request, send_lock):
        await self._handle_team_project_delete(ws, request, send_lock)

    async def handle_team_project_archive_for_test(self, ws, request, send_lock):
        await self._handle_team_project_archive(ws, request, send_lock)

    async def handle_team_members_list_for_test(self, ws, request, send_lock):
        await self._handle_team_members_list(ws, request, send_lock)

    async def handle_team_member_remove_for_test(self, ws, request, send_lock):
        await self._handle_team_member_remove(ws, request, send_lock)

    async def handle_team_member_role_for_test(self, ws, request, send_lock):
        await self._handle_team_member_role(ws, request, send_lock)

    async def handle_team_invite_create_for_test(self, ws, request, send_lock):
        await self._handle_team_invite_create(ws, request, send_lock)

    async def handle_team_invite_accept_for_test(self, ws, request, send_lock):
        await self._handle_team_invite_accept(ws, request, send_lock)

    async def handle_team_message_send_for_test(self, ws, request, send_lock):
        await self._handle_team_message_send(ws, request, send_lock)

    async def handle_team_message_list_for_test(self, ws, request, send_lock):
        await self._handle_team_message_list(ws, request, send_lock)

    async def handle_team_message_subscribe_for_test(self, ws, request, send_lock):
        await self._handle_team_message_subscribe(ws, request, send_lock)

    async def handle_team_message_unsubscribe_for_test(self, ws, request, send_lock):
        await self._handle_team_message_unsubscribe(ws, request, send_lock)

    async def handle_team_whoami_for_test(self, ws, request, send_lock):
        await self._handle_team_whoami(ws, request, send_lock)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(monkeypatch) -> Harness:
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    return Harness()


def _make_request(method: ReqMethod, params: dict) -> AgentRequest:
    return AgentRequest(
        request_id="req-projects",
        channel_id="web",
        req_method=method,
        params=params,
    )


# ── team.create ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_team_create_returns_project_on_success(server, monkeypatch) -> None:
    captured: dict = {}

    async def fake_create_group(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"group_id": 7, "name": "proj", "owner_dataset": "d", "seq_counter": 1}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )

    ws = FakeWebSocket()
    await server.handle_team_create_for_test(
        ws, _make_request(ReqMethod.TEAM_CREATE, {"name": "proj", "max_members": 20}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["project"] == {
        "group_id": 7,
        "name": "proj",
        "owner_dataset": "d",
        "seq_counter": 1,
    }
    assert captured["name"] == "proj"
    assert captured["max_members"] == 20


@pytest.mark.anyio
async def test_team_create_works_outside_cluster_mode(server, monkeypatch) -> None:
    """The projects page must work from any mode — no UNSUPPORTED_MODE gate."""

    async def fake_create_group(**kwargs):  # noqa: ANN003
        return {"group_id": 1, "name": "proj"}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )

    ws = FakeWebSocket()
    # Explicitly a non-team mode; team.delete would reject this with UNSUPPORTED_MODE.
    await server.handle_team_create_for_test(
        ws,
        _make_request(ReqMethod.TEAM_CREATE, {"name": "proj", "mode": "agent"}),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["project"]["group_id"] == 1


@pytest.mark.anyio
async def test_team_create_missing_name_is_bad_request(server, monkeypatch) -> None:
    """A blank name is rejected locally, before the relay is contacted.

    The handler delegates argument validation to ``create_group`` (which
    validates through the client), so this drives the real validation and only
    stubs the network layer.
    """

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("must not call the relay without a name")

    from jiuwenswarm.agents.harness.team.groupchat import (
        AsyncGroupChatClient,
        GroupChatConfig,
    )
    from jiuwenswarm.agents.harness.team.groupchat.client import (
        _build_headers,
        _normalize_base_url,
    )

    import httpx

    async def fake_create_group(**kwargs):  # noqa: ANN003
        config = GroupChatConfig(
            enabled=True,
            base_url="http://relay.test:8000",
            api_key="k",
            timeout=5.0,
            default_max_members=50,
        )
        client = AsyncGroupChatClient(config)
        client._client = httpx.AsyncClient(
            base_url=_normalize_base_url(config.base_url),
            timeout=config.timeout,
            headers=_build_headers(config.api_key),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.create_group(**kwargs)
        finally:
            await client.aclose()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )

    ws = FakeWebSocket()
    await server.handle_team_create_for_test(
        ws, _make_request(ReqMethod.TEAM_CREATE, {}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "invalid_request"


@pytest.mark.anyio
async def test_team_create_propagates_not_configured(server, monkeypatch) -> None:
    async def fake_create_group(**kwargs):  # noqa: ANN003
        raise GroupChatNotConfiguredError()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )

    ws = FakeWebSocket()
    await server.handle_team_create_for_test(
        ws, _make_request(ReqMethod.TEAM_CREATE, {"name": "proj"}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "not_configured"


@pytest.mark.anyio
async def test_team_create_propagates_relay_code(server, monkeypatch) -> None:
    async def fake_create_group(**kwargs):  # noqa: ANN003
        raise GroupChatError("full", code="groupchat_group_quota_exceeded", status_code=429)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )

    ws = FakeWebSocket()
    await server.handle_team_create_for_test(
        ws, _make_request(ReqMethod.TEAM_CREATE, {"name": "proj"}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "groupchat_group_quota_exceeded"


@pytest.mark.anyio
async def test_team_create_unexpected_error_is_contained(server, monkeypatch) -> None:
    """An unexpected exception must become a response, not a broken request loop."""

    async def fake_create_group(**kwargs):  # noqa: ANN003
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )

    ws = FakeWebSocket()
    await server.handle_team_create_for_test(
        ws, _make_request(ReqMethod.TEAM_CREATE, {"name": "proj"}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "CREATE_FAILED"


# ── team.projects.list ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_projects_list_returns_groups(server, monkeypatch) -> None:
    async def fake_list_groups(**kwargs):  # noqa: ANN003
        return [{"group_id": 1, "name": "a"}, {"group_id": 2, "name": "b"}]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_groups", fake_list_groups
    )

    ws = FakeWebSocket()
    await server.handle_team_projects_list_for_test(
        ws, _make_request(ReqMethod.TEAM_PROJECTS_LIST, {}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    assert [p["group_id"] for p in ws.sent[0]["payload"]["projects"]] == [1, 2]


@pytest.mark.anyio
async def test_projects_list_propagates_unavailable(server, monkeypatch) -> None:
    async def fake_list_groups(**kwargs):  # noqa: ANN003
        raise GroupChatUnavailableError("cannot connect")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_groups", fake_list_groups
    )

    ws = FakeWebSocket()
    await server.handle_team_projects_list_for_test(
        ws, _make_request(ReqMethod.TEAM_PROJECTS_LIST, {}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "unavailable"


# ── team.whoami ──────────────────────────────────────────────────────────────


def _whoami_config(*, agent_configured: bool):
    """A groupchat config whose agent identity is present or absent."""
    from jiuwenswarm.agents.harness.team.groupchat import (
        GroupChatAgentConfig,
        GroupChatConfig,
    )

    return GroupChatConfig(
        enabled=True,
        base_url="http://relay.test:8000",
        api_key="human-key",
        timeout=5.0,
        default_max_members=50,
        agent=GroupChatAgentConfig(
            enabled=agent_configured,
            api_key="agent-key" if agent_configured else None,
            display_name="AI 助手",
        ),
    )


@pytest.mark.anyio
async def test_whoami_reports_both_local_principals(server, monkeypatch) -> None:
    """The panel needs both ids to put its agent's messages on its own side."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.resolve_groupchat_config",
        lambda *a, **k: _whoami_config(agent_configured=True),
    )

    async def fake_whoami(**kwargs):  # noqa: ANN003
        # The agent credential answers for the agent; the human credential (no
        # config_base override) for the person.
        if kwargs.get("config_base"):
            return {"principal_id": "u_agent", "handle": "agent-default"}
        return {"principal_id": "u_human", "handle": "root"}

    monkeypatch.setattr("jiuwenswarm.agents.harness.team.groupchat.whoami", fake_whoami)

    ws = FakeWebSocket()
    await server.handle_team_whoami_for_test(
        ws, _make_request(ReqMethod.TEAM_WHOAMI, {}), asyncio.Lock()
    )

    payload = ws.sent[0]["payload"]
    assert ws.sent[0]["ok"] is True
    assert payload["principal_id"] == "u_human"
    assert payload["agent_principal_id"] == "u_agent"


@pytest.mark.anyio
async def test_whoami_reports_no_agent_identity_when_the_agent_shares_it(server, monkeypatch) -> None:
    """A shared-credential setup has one identity, so there is no second side."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.resolve_groupchat_config",
        lambda *a, **k: _whoami_config(agent_configured=False),
    )

    calls: list = []

    async def fake_whoami(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return {"principal_id": "u_human", "handle": "root"}

    monkeypatch.setattr("jiuwenswarm.agents.harness.team.groupchat.whoami", fake_whoami)

    ws = FakeWebSocket()
    await server.handle_team_whoami_for_test(
        ws, _make_request(ReqMethod.TEAM_WHOAMI, {}), asyncio.Lock()
    )

    payload = ws.sent[0]["payload"]
    assert ws.sent[0]["ok"] is True
    assert payload["agent_principal_id"] == ""
    # The agent credential must not be consulted at all when there is none.
    assert calls == [{}]


# ── members ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_members_list_returns_rows(server, monkeypatch) -> None:
    async def fake_list_members(**kwargs):  # noqa: ANN003
        assert kwargs["group_id"] == 3
        return [{"principal_id": "u_a", "role": "owner"}, {"principal_id": "u_b"}]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_members", fake_list_members
    )

    ws = FakeWebSocket()
    await server.handle_team_members_list_for_test(
        ws, _make_request(ReqMethod.TEAM_MEMBERS_LIST, {"group_id": 3}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    assert [m["principal_id"] for m in ws.sent[0]["payload"]["members"]] == ["u_a", "u_b"]


@pytest.mark.anyio
async def test_member_remove_propagates_relay_code(server, monkeypatch) -> None:
    async def fake_remove(**kwargs):  # noqa: ANN003
        raise GroupChatError(
            "cannot remove the owner", code="groupchat_owner_immutable", status_code=403
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.remove_member", fake_remove
    )

    ws = FakeWebSocket()
    await server.handle_team_member_remove_for_test(
        ws,
        _make_request(ReqMethod.TEAM_MEMBER_REMOVE, {"group_id": 3, "principal_id": "u_a"}),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "groupchat_owner_immutable"


@pytest.mark.anyio
async def test_member_role_rejects_bogus_role_before_the_relay(server, monkeypatch) -> None:
    """The client validates the role, so a bad one never becomes an HTTP call."""
    import httpx

    from jiuwenswarm.agents.harness.team.groupchat import AsyncGroupChatClient, GroupChatConfig
    from jiuwenswarm.agents.harness.team.groupchat.client import (
        _build_headers,
        _normalize_base_url,
    )

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("an invalid role must not reach the relay")

    async def fake_set_role(**kwargs):  # noqa: ANN003
        config = GroupChatConfig(
            enabled=True,
            base_url="http://relay.test:8000",
            api_key="k",
            timeout=5.0,
            default_max_members=50,
        )
        client = AsyncGroupChatClient(config)
        client._client = httpx.AsyncClient(
            base_url=_normalize_base_url(config.base_url),
            timeout=config.timeout,
            headers=_build_headers(config.api_key),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.set_member_role(**kwargs)
        finally:
            await client.aclose()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.set_member_role", fake_set_role
    )

    ws = FakeWebSocket()
    await server.handle_team_member_role_for_test(
        ws,
        _make_request(
            ReqMethod.TEAM_MEMBER_ROLE,
            {"group_id": 3, "principal_id": "u_b", "role": "superuser"},
        ),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "invalid_request"


# ── invites ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_targeted_invite_returns_a_token_without_joining(server, monkeypatch) -> None:
    """A targeted invite is redeemed by the invitee, never by the creator.

    The relay refuses anyone but the named principal (``groupchat_invite_not_for_caller``),
    so creation must only hand back the token.
    """

    async def fake_create_invite(**kwargs):  # noqa: ANN003
        assert kwargs["invitee_id"] == "u_b"
        return {"mode": "targeted", "invitee_id": "u_b", "token": "tok-1"}

    async def fake_accept_invite(**kwargs):  # noqa: ANN003  # pragma: no cover
        raise AssertionError("the creator must not redeem a targeted invite")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_invite", fake_create_invite
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.accept_invite", fake_accept_invite
    )

    ws = FakeWebSocket()
    await server.handle_team_invite_create_for_test(
        ws,
        _make_request(ReqMethod.TEAM_INVITE_CREATE, {"group_id": 3, "invitee_id": "u_b"}),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["invite"]["token"] == "tok-1"
    assert ws.sent[0]["payload"]["invite"]["mode"] == "targeted"
    assert "joined" not in ws.sent[0]["payload"]


@pytest.mark.anyio
async def test_invite_accept_is_its_own_rpc(server, monkeypatch) -> None:
    """The invitee redeems the token through ``team.invite.accept``."""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    captured: dict = {}

    async def fake_accept_invite(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"role": "member", "joined_seq": 4}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.accept_invite", fake_accept_invite
    )

    class _AcceptHarness(AgentWebSocketServer):
        async def run(self, ws, request, send_lock):  # noqa: ANN001
            await self._handle_team_invite_accept(ws, request, send_lock)

    ws = FakeWebSocket()
    await _AcceptHarness().run(
        ws,
        _make_request(ReqMethod.TEAM_INVITE_ACCEPT, {"token": "tok-1"}),
        asyncio.Lock(),
    )

    assert captured == {"token": "tok-1"}
    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["role"] == "member"


# ── agent auto-join on both membership paths ─────────────────────────────────


def _agent_config(*, api_key: str | None = "a2x_pat_agent", display_name: str = ""):
    """A configured relay with a distinct agent identity."""
    from jiuwenswarm.agents.harness.team.groupchat import (
        GroupChatAgentConfig,
        GroupChatConfig,
    )

    return GroupChatConfig(
        enabled=True,
        base_url="http://relay.test:8000",
        api_key="a2x_pat_human",
        timeout=5.0,
        default_max_members=50,
        agent=GroupChatAgentConfig(
            enabled=True, api_key=api_key, display_name=display_name
        ),
    )


def _stub_join_primitives(
    monkeypatch, *, config, principal="u_agent", members=None
) -> dict:
    """Patch the relay + config surface the auto-join touches.

    Everything ``_try_join_agent_to_group`` imports is patched at the package
    attribute, because the handler imports these names lazily inside the call.
    """
    calls: dict = {"invites": [], "accepted": [], "attached": []}

    async def fake_whoami(**kwargs):  # noqa: ANN003
        calls["whoami_config"] = kwargs.get("config_base")
        return {"principal_id": principal} if principal else {}

    async def fake_list_members(**kwargs):  # noqa: ANN003
        calls["members_group_id"] = kwargs.get("group_id")
        return list(members or [])

    async def fake_create_invite(**kwargs):  # noqa: ANN003
        calls["invites"].append(kwargs)
        return {"token": "inv-token", "mode": "targeted"}

    async def fake_accept_invite(**kwargs):  # noqa: ANN003
        calls["accepted"].append(kwargs)
        # The relay nests the group under "group", with its group_id/name inside.
        return {
            "group": {"group_id": 42, "name": "proj"},
            "role": "member",
            "joined_seq": 4,
            "recent_messages": [],
        }

    class _FakeBridge:
        def __init__(self) -> None:
            self.attached: list[tuple] = []

        async def attach_group(self, group_id, *, name=""):  # noqa: ANN001
            self.attached.append((group_id, name))
            calls["attached"].append((group_id, name))

    bridge = _FakeBridge()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.resolve_groupchat_config",
        lambda *a, **k: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.whoami", fake_whoami
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_members", fake_list_members
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_invite", fake_create_invite
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.accept_invite", fake_accept_invite
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.get_bridge", lambda: bridge
    )
    calls["bridge"] = bridge
    return calls


@pytest.mark.anyio
async def test_accepting_an_invite_also_joins_the_agent(server, monkeypatch) -> None:
    """Joining a project must put the agent in the group, not just the human.

    This is the defect the join path had: accepting a token made only the human
    credential a member, so a project created elsewhere never got this
    instance's agent and @-mentions could not reach it.
    """
    calls = _stub_join_primitives(monkeypatch, config=_agent_config())

    ws = FakeWebSocket()
    await server.handle_team_invite_accept_for_test(
        ws, _make_request(ReqMethod.TEAM_INVITE_ACCEPT, {"token": "tok-1"}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    # The agent got its own targeted invite and redeemed it with its own key.
    # Two redeems: the human's (the accept the caller asked for) and the agent's.
    assert calls["invites"] == [{"group_id": 42, "invitee_id": "u_agent"}]
    assert calls["accepted"][0]["token"] == "tok-1"
    agent_redeem = calls["accepted"][1]
    assert agent_redeem["token"] == "inv-token"
    assert agent_redeem["config_base"]["groupchat"]["api_key"] == "a2x_pat_agent"
    # And the bridge now watches it.
    assert calls["bridge"].attached == [(42, "proj")]
@pytest.mark.anyio
async def test_accepting_an_invite_without_a_group_id_still_succeeds(
    server, monkeypatch
) -> None:
    """A redeem that reports no group id must not break the accept response."""
    calls = _stub_join_primitives(monkeypatch, config=_agent_config())

    async def fake_accept_invite(**kwargs):  # noqa: ANN003
        return {"role": "member"}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.accept_invite", fake_accept_invite
    )

    ws = FakeWebSocket()
    await server.handle_team_invite_accept_for_test(
        ws, _make_request(ReqMethod.TEAM_INVITE_ACCEPT, {"token": "tok-1"}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    assert calls["invites"] == []


@pytest.mark.anyio
async def test_auto_join_is_skipped_when_the_agent_shares_the_human_key(
    server, monkeypatch
) -> None:
    """One identity means the human member already is the agent."""
    calls = _stub_join_primitives(monkeypatch, config=_agent_config(api_key=None))

    ws = FakeWebSocket()
    await server.handle_team_invite_accept_for_test(
        ws, _make_request(ReqMethod.TEAM_INVITE_ACCEPT, {"token": "tok-1"}), asyncio.Lock()
    )

    assert calls["invites"] == []
    assert calls["bridge"].attached == []


@pytest.mark.anyio
async def test_auto_join_attaches_without_reinviting_an_existing_member(
    server, monkeypatch
) -> None:
    """An agent already in the group is attached, not invited again."""
    calls = _stub_join_primitives(
        monkeypatch,
        config=_agent_config(),
        members=[{"principal_id": "u_agent", "role": "member"}],
    )

    ws = FakeWebSocket()
    await server.handle_team_invite_accept_for_test(
        ws, _make_request(ReqMethod.TEAM_INVITE_ACCEPT, {"token": "tok-1"}), asyncio.Lock()
    )

    assert calls["invites"] == [], "an existing member must not be re-invited"
    assert calls["bridge"].attached == [(42, "proj")]
    # The membership probe ran against the joined group.
    assert calls["members_group_id"] == 42


@pytest.mark.anyio
async def test_auto_join_survives_an_unreadable_member_list(server, monkeypatch) -> None:
    """A failed membership probe must fall through to the join, not crash it.

    The probe swallows the relay's error and answers "not a member"; if that
    handler is not reachable (a missing import, say) the exception escapes the
    join entirely, so this pins the behaviour rather than the implementation.
    """
    from jiuwenswarm.agents.harness.team.groupchat import GroupChatError

    calls = _stub_join_primitives(monkeypatch, config=_agent_config())

    async def boom(**kwargs):  # noqa: ANN003
        raise GroupChatError("members unavailable", code="groupchat_group_not_found")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_members", boom
    )

    ws = FakeWebSocket()
    await server.handle_team_invite_accept_for_test(
        ws, _make_request(ReqMethod.TEAM_INVITE_ACCEPT, {"token": "tok-1"}), asyncio.Lock()
    )

    # The probe failed, so the join proceeded instead of being skipped.
    assert ws.sent[0]["ok"] is True
    assert calls["invites"] == [{"group_id": 42, "invitee_id": "u_agent"}]
    assert calls["bridge"].attached == [(42, "proj")]


@pytest.mark.anyio
async def test_auto_join_error_does_not_fail_creation(server, monkeypatch) -> None:
    """A relay failure on the agent's join must not discard a created group."""

    async def fake_create_group(**kwargs):  # noqa: ANN003
        return {"group_id": 9, "name": "proj"}

    async def boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("relay exploded")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_group", fake_create_group
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.resolve_groupchat_config", boom
    )

    ws = FakeWebSocket()
    await server.handle_team_create_for_test(
        ws, _make_request(ReqMethod.TEAM_CREATE, {"name": "proj"}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["project"]["group_id"] == 9


# ── member listing marks the agent ───────────────────────────────────────────


@pytest.mark.anyio
async def test_members_list_marks_the_agent_row(server, monkeypatch) -> None:
    """The agent's row is flagged and named; human rows are left unmarked."""
    _stub_join_primitives(
        monkeypatch, config=_agent_config(display_name="研究助手")
    )

    async def fake_list_members(**kwargs):  # noqa: ANN003
        # The relay names an Agent after its owner, so both rows read "root";
        # only the badge tells them apart.
        return [
            {"principal_id": "u_human", "role": "owner", "display_name": "root"},
            {"principal_id": "u_agent", "role": "member", "is_agent": True,
             "display_name": "root"},
        ]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_members", fake_list_members
    )

    ws = FakeWebSocket()
    await server.handle_team_members_list_for_test(
        ws, _make_request(ReqMethod.TEAM_MEMBERS_LIST, {"group_id": 3}), asyncio.Lock()
    )

    rows = ws.sent[0]["payload"]["members"]
    assert ws.sent[0]["ok"] is True
    assert rows[1]["is_agent"] is True
    # The local overlay must not rename the Agent: its name is its owner's.
    assert rows[1]["display_name"] == "root"
    assert rows[0]["display_name"] == "root"
    # A local "not mine" is not evidence of "not an agent": the human row is
    # left exactly as the relay sent it rather than written to False.
    assert not rows[0].get("is_agent")


@pytest.mark.anyio
async def test_members_list_keeps_the_relays_agent_marks(server, monkeypatch) -> None:
    """The relay's mark and name are passed through; the badge is the only tell."""
    _stub_join_primitives(
        monkeypatch, config=_agent_config(display_name="本实例助手")
    )

    async def fake_list_members(**kwargs):  # noqa: ANN003
        # Each Agent is named after its owner, so a peer's Agent reads as that
        # peer's name — not as a local label, and not as its own handle.
        return [
            {"principal_id": "u_peer_agent", "role": "member", "is_agent": True,
             "display_name": "peer-owner"},
            {"principal_id": "u_agent", "role": "member", "is_agent": True,
             "display_name": "root"},
            {"principal_id": "u_human", "role": "owner", "display_name": "root"},
        ]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_members", fake_list_members
    )

    ws = FakeWebSocket()
    await server.handle_team_members_list_for_test(
        ws, _make_request(ReqMethod.TEAM_MEMBERS_LIST, {"group_id": 3}), asyncio.Lock()
    )

    rows = ws.sent[0]["payload"]["members"]
    # Peer agents keep their mark; the human row is left without the key
    # entirely — a local "not mine" must not be written as "not an agent".
    assert [r.get("is_agent") for r in rows] == [True, True, None]
    # Names come straight from the relay: the local configured label
    # ("本实例助手") must NOT replace the owner's name on its own Agent row.
    assert rows[0]["display_name"] == "peer-owner"
    assert rows[1]["display_name"] == "root"
    assert rows[1]["display_name"] != "本实例助手"
    assert rows[2]["display_name"] == "root"


@pytest.mark.anyio
async def test_members_list_is_plain_when_the_agent_identity_is_unresolvable(
    server, monkeypatch
) -> None:
    """An unconfigured relay leaves rows as the relay sent them."""
    from jiuwenswarm.agents.harness.team.groupchat import (
        GroupChatAgentConfig,
        GroupChatConfig,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.resolve_groupchat_config",
        lambda *a, **k: GroupChatConfig(
            enabled=False,
            base_url="",
            api_key=None,
            timeout=5.0,
            default_max_members=50,
            agent=GroupChatAgentConfig(),
        ),
    )

    async def fake_list_members(**kwargs):  # noqa: ANN003
        return [{"principal_id": "u_human", "role": "owner"}]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.list_members", fake_list_members
    )

    ws = FakeWebSocket()
    await server.handle_team_members_list_for_test(
        ws, _make_request(ReqMethod.TEAM_MEMBERS_LIST, {"group_id": 3}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["members"] == [
        {"principal_id": "u_human", "role": "owner"}
    ]


@pytest.mark.anyio
async def test_open_invite_is_not_auto_accepted(server, monkeypatch) -> None:
    """An open invite returns a share token for someone else to redeem."""

    async def fake_create_invite(**kwargs):  # noqa: ANN003
        return {"mode": "open", "token": "share-token", "max_uses": 5}

    async def fake_accept_invite(**kwargs):  # noqa: ANN003  # pragma: no cover
        raise AssertionError("an open invite must not be redeemed by the caller")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.create_invite", fake_create_invite
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.accept_invite", fake_accept_invite
    )

    ws = FakeWebSocket()
    await server.handle_team_invite_create_for_test(
        ws,
        _make_request(ReqMethod.TEAM_INVITE_CREATE, {"group_id": 3, "max_uses": 5}),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["invite"]["token"] == "share-token"
    assert "joined" not in ws.sent[0]["payload"]


# ── messages ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_message_send_passes_idempotency_key(server, monkeypatch) -> None:
    captured: dict = {}

    async def fake_send(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"seq": 9, "message_id": "m-9"}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.send_message", fake_send
    )

    ws = FakeWebSocket()
    await server.handle_team_message_send_for_test(
        ws,
        _make_request(
            ReqMethod.TEAM_MESSAGE_SEND,
            {"group_id": 3, "text": "hello", "client_msg_id": "cid-1"},
        ),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is True
    assert captured["text"] == "hello"
    assert captured["client_msg_id"] == "cid-1"


@pytest.mark.anyio
async def test_message_list_returns_page(server, monkeypatch) -> None:
    async def fake_fetch(**kwargs):  # noqa: ANN003
        assert kwargs["group_id"] == 3
        assert kwargs["limit"] == 20
        return {"messages": [{"seq": 1}], "next_seq": 2, "has_more": False, "latest_seq": 1}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.fetch_messages", fake_fetch
    )

    ws = FakeWebSocket()
    await server.handle_team_message_list_for_test(
        ws,
        _make_request(ReqMethod.TEAM_MESSAGE_LIST, {"group_id": 3, "limit": 20}),
        asyncio.Lock(),
    )

    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["messages"] == [{"seq": 1}]


# ── subscription ─────────────────────────────────────────────────────────────


class _StubHub:
    """Records attach/detach without opening a real relay socket."""

    def __init__(self) -> None:
        self.watched_groups: list[int] = []
        self.calls: dict = {}

    async def attach(self, *, group_id, subscriber_id):  # noqa: ANN001
        self.calls["attach"] = (group_id, subscriber_id)
        if group_id not in self.watched_groups:
            self.watched_groups.append(group_id)

    async def detach(self, *, group_id, subscriber_id):  # noqa: ANN001
        self.calls["detach"] = (group_id, subscriber_id)
        if group_id in self.watched_groups:
            self.watched_groups.remove(group_id)


@pytest.mark.anyio
async def test_subscribe_and_unsubscribe_manage_the_hub(server, monkeypatch) -> None:
    from jiuwenswarm.agents.harness.team.groupchat import reset_subscription_hub_for_tests

    reset_subscription_hub_for_tests()
    monkeypatch.setattr(
        agent_ws_server_module, "_ensure_group_hub_callback", lambda hub: None
    )
    hub = _StubHub()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.get_subscription_hub", lambda: hub
    )

    ws = FakeWebSocket()
    await server.handle_team_message_subscribe_for_test(
        ws, _make_request(ReqMethod.TEAM_MESSAGE_SUBSCRIBE, {"group_id": 3}), asyncio.Lock()
    )
    assert ws.sent[0]["ok"] is True
    assert hub.calls["attach"][0] == 3
    assert ws.sent[0]["payload"]["watched"] == [3]

    await server.handle_team_message_unsubscribe_for_test(
        ws, _make_request(ReqMethod.TEAM_MESSAGE_UNSUBSCRIBE, {"group_id": 3}), asyncio.Lock()
    )
    assert ws.sent[1]["ok"] is True
    assert hub.calls["detach"][0] == 3
    assert ws.sent[1]["payload"]["watched"] == []


@pytest.mark.anyio
async def test_subscribe_rejects_a_missing_group_id(server, monkeypatch) -> None:
    from jiuwenswarm.agents.harness.team.groupchat import reset_subscription_hub_for_tests

    reset_subscription_hub_for_tests()
    monkeypatch.setattr(
        agent_ws_server_module, "_ensure_group_hub_callback", lambda hub: None
    )

    ws = FakeWebSocket()
    await server.handle_team_message_subscribe_for_test(
        ws, _make_request(ReqMethod.TEAM_MESSAGE_SUBSCRIBE, {}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "invalid_request"


# ── project delete / archive ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_project_delete_returns_relay_payload(server, monkeypatch) -> None:
    captured: dict = {}

    async def fake_delete(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"group_id": 3, "status": "deleted", "dropped_connections": 2}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.delete_group", fake_delete
    )

    ws = FakeWebSocket()
    await server.handle_team_project_delete_for_test(
        ws, _make_request(ReqMethod.TEAM_PROJECT_DELETE, {"group_id": 3}), asyncio.Lock()
    )

    assert captured == {"group_id": 3}
    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["status"] == "deleted"


@pytest.mark.anyio
async def test_project_delete_propagates_owner_required(server, monkeypatch) -> None:
    """Only the owner may delete; the relay's code must survive to the UI."""

    async def fake_delete(**kwargs):  # noqa: ANN003
        raise GroupChatError(
            "Only the group owner may delete a group",
            code="groupchat_owner_required",
            status_code=403,
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.delete_group", fake_delete
    )

    ws = FakeWebSocket()
    await server.handle_team_project_delete_for_test(
        ws, _make_request(ReqMethod.TEAM_PROJECT_DELETE, {"group_id": 3}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "groupchat_owner_required"


@pytest.mark.anyio
async def test_project_delete_missing_group_is_not_found(server, monkeypatch) -> None:
    async def fake_delete(**kwargs):  # noqa: ANN003
        raise GroupChatError(
            "Group not found", code="groupchat_group_not_found", status_code=404
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.delete_group", fake_delete
    )

    ws = FakeWebSocket()
    await server.handle_team_project_delete_for_test(
        ws, _make_request(ReqMethod.TEAM_PROJECT_DELETE, {}), asyncio.Lock()
    )

    assert ws.sent[0]["ok"] is False
    assert ws.sent[0]["payload"]["code"] == "groupchat_group_not_found"


@pytest.mark.anyio
async def test_project_archive_defaults_to_archiving(server, monkeypatch) -> None:
    """Omitting ``archived`` archives rather than unarchiving."""
    captured: dict = {}

    async def fake_archive(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"group_id": 3, "archived": True}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.set_archived", fake_archive
    )

    ws = FakeWebSocket()
    await server.handle_team_project_archive_for_test(
        ws, _make_request(ReqMethod.TEAM_PROJECT_ARCHIVE, {"group_id": 3}), asyncio.Lock()
    )

    assert captured == {"group_id": 3, "archived": True}
    assert ws.sent[0]["ok"] is True


@pytest.mark.anyio
async def test_project_archive_can_unarchive(server, monkeypatch) -> None:
    captured: dict = {}

    async def fake_archive(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"group_id": 3, "archived": False}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.groupchat.set_archived", fake_archive
    )

    ws = FakeWebSocket()
    await server.handle_team_project_archive_for_test(
        ws,
        _make_request(ReqMethod.TEAM_PROJECT_ARCHIVE, {"group_id": 3, "archived": False}),
        asyncio.Lock(),
    )

    assert captured == {"group_id": 3, "archived": False}
    assert ws.sent[0]["ok"] is True
