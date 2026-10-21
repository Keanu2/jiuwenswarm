# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Outbound A2A file tools + A2X registry query tool.

Ports the two OpenClaw ``a2a-gateway`` agent tools (``a2a_send_file`` and
``a2a_send_local_file``) into JiuwenSwarm's openjiuwen tool framework, and
adds ``a2x_list_agents`` — a read-only query tool so a conversational agent
can discover which agents are currently online in the A2X registry.

Each file tool resolves the active peer from the gateway config, builds the
outbound message (with a FilePart for file payloads or a text Part for
prompts), and delegates to the process-wide :class:`A2AOutboundClient`.
The query tool instead opens a short-lived A2X registry client, lists the
services, and returns a human-readable summary.

Tool construction follows the ``SendFileToolkit`` pattern: a toolkit class
exposes :meth:`get_tools` returning ``list[Tool]`` (``LocalFunction``
instances), which the swarm provider registers with the agent runner.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from .runtime import get_outbound_client

logger = logging.getLogger(__name__)

# MIME sniff fallbacks keyed by extension.
_MIME_BY_EXT: dict[str, str] = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".json": "application/json", ".csv": "text/csv", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".zip": "application/zip", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _guess_mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


class A2ASendFileToolkit:
    """Toolkit exposing ``a2a_send_file`` / ``a2a_send_local_file`` + ``a2x_list_agents``.

    The two file tools send a message to a configured A2A peer. The file tool
    accepts either a local absolute path (read, base64-encode, send as a
    FilePart with the basename as the transport name) or a publicly-reachable
    file URI (sent as a URL FilePart). Mirrors OpenClaw ``index.ts`` tool
    registration.

    ``a2x_list_agents`` is a read-only query: it lists agents currently
    registered in the A2X registry (optionally across every dataset) so a
    conversational agent can answer "which agents are online / what remote
    agents can I reach". It never mutates registry state.
    """

    def __init__(self) -> None:
        # No per-request state: peers/config resolve at call time from the
        # process-wide outbound client, which holds the A2AGatewayConfig.
        pass

    def get_tools(self) -> list[Tool]:
        """Return the outbound A2A file tools + the A2X query tool."""

        def make_tool(name: str, description: str, input_params: dict, func) -> Tool:
            return LocalFunction(card=ToolCard(name=name, description=description,
                                               input_params=input_params), func=func)

        return [
            make_tool(
                name="a2a_send_file",
                description=(
                    "【A2A 发送文件工具】向指定的 A2A peer 发送文件(URI 或本地路径)。"
                    "使用场景：需要把文件交付给另一台服务器上的 agent，或触发远端 agent 处理某个文件。"
                    "参数：peer_name 为目标 peer 名称(需在 a2a_gateway.peers 配置)，"
                    "file_uri 为公网可访问的文件 URI，abs_file_path 为本机绝对路径(二选一)，"
                    "agent_id 可选(指定远端路由目标 agent，非标准扩展)。"
                    "返回远端 agent 的文本响应。单文件上限 50MB。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "peer_name": {
                            "type": "string",
                            "description": "目标 A2A peer 名称(对应 a2a_gateway.peers[].name)。",
                        },
                        "file_uri": {
                            "type": "string",
                            "description": "公网可访问的文件 URI。与 abs_file_path 二选一。",
                        },
                        "abs_file_path": {
                            "type": "string",
                            "description": "本机文件绝对路径。与 file_uri 二选一。传输名恒为 basename。",
                        },
                        "agent_id": {
                            "type": "string",
                            "description": "可选：远端路由目标 agentId(非标准扩展，JSON-RPC/REST 最可靠)。",
                        },
                        "message": {
                            "type": "string",
                            "description": "可选：随文件发送的提示文本。",
                        },
                    },
                    "required": ["peer_name"],
                },
                func=self.a2a_send_file,
            ),
            make_tool(
                name="a2a_send_local_file",
                description=(
                    "【A2A 本地文件发送工具】按本机路径向指定 A2A peer 发送文件。"
                    "使用场景：文件在本机、避免公网 URI 与命令行塞大段 base64 时使用。"
                    "参数：peer_name 为目标 peer 名称，abs_file_path 为本机绝对路径(必需)，"
                    "agent_id 可选。传输名恒为文件 basename(忽略 name 参数，防模型编造)。"
                    "单文件上限 50MB，超出拒绝。返回远端 agent 的文本响应。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "peer_name": {
                            "type": "string",
                            "description": "目标 A2A peer 名称(对应 a2a_gateway.peers[].name)。",
                        },
                        "abs_file_path": {
                            "type": "string",
                            "description": "本机文件绝对路径(必需)。",
                        },
                        "agent_id": {
                            "type": "string",
                            "description": "可选：远端路由目标 agentId。",
                        },
                        "message": {
                            "type": "string",
                            "description": "可选：随文件发送的提示文本。",
                        },
                    },
                    "required": ["peer_name", "abs_file_path"],
                },
                func=self.a2a_send_local_file,
            ),
            make_tool(
                name="a2x_list_agents",
                description=(
                    "【A2X 注册中心查询工具】查询 A2X 注册中心里当前已注册的 Agent（默认只看在线的）。"
                    "使用场景：用户问「有哪些 agent 在线 / 注册了哪些 agent / 能调用哪些远端 agent / "
                    "团队里有哪些成员」时使用。参数：dataset 可选(默认取配置的 team_pool 数据集)，"
                    "status 可选(online|busy|offline|all，默认 online，all 表示不限状态)，"
                    "include_all_datasets 可选(true 时枚举注册中心所有数据集聚合返回，默认 false)。"
                    "返回每个 agent 的 name / description / endpoint / status / id，"
                    "若 agent 带隧道还会标注 tunnelDeviceId。只读，不修改注册中心。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "dataset": {
                            "type": "string",
                            "description": "要查询的 A2X 数据集名。默认取 react.a2x_registry.dataset(通常为 team_pool)。",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["online", "busy", "offline", "all"],
                            "description": "按状态过滤，默认 online。all 表示不限状态、返回全部已注册 agent。",
                        },
                        "include_all_datasets": {
                            "type": "boolean",
                            "description": "true 时忽略 dataset，枚举注册中心所有数据集合并聚合返回。默认 false。",
                        },
                    },
                    "required": [],
                },
                func=self.a2x_list_agents,
            ),
            make_tool(
                name="a2a_send_message",
                description=(
                    "【A2A 发消息工具】向 A2X 注册中心里的指定在线 Agent 发送文本消息并返回其响应。"
                    "使用场景：用户要『向某 agent 发消息 / 跟远端 agent 对话 / 让 X 回应 hello』时使用。"
                    "参数：agent_name 为目标 agent 的 name 或 id(支持部分匹配，如 HW-Phone1 命中 HW-Phone1-Agent1)；"
                    "message 为要发送的文本；dataset 可选(默认取 react.a2x_registry.dataset，通常为 openclaw_devices)。"
                    "工具会先在注册中心查到目标 agent 的 endpoint 与隧道设备 ID，再经 A2A 出站客户端(必要时经隧道转发)发送。"
                    "返回远端 agent 的文本响应；目标未找到会列出可用 agent。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "目标 agent 的 name 或 id(支持部分匹配)。",
                        },
                        "message": {
                            "type": "string",
                            "description": "要发送的文本消息。",
                        },
                        "dataset": {
                            "type": "string",
                            "description": "查询目标 agent 的 A2X 数据集，默认取 react.a2x_registry.dataset。",
                        },
                    },
                    "required": ["agent_name", "message"],
                },
                func=self.a2a_send_message,
            ),
        ]

    async def a2a_send_file(
        self,
        peer_name: str,
        file_uri: str = "",
        abs_file_path: str = "",
        agent_id: str = "",
        message: str = "",
    ) -> str:
        """Send a file (URI or local path) to an A2A peer."""
        client = get_outbound_client()
        if client is None:
            return "A2A 出站未启用：未找到 outbound 客户端(请设置 A2A_GATEWAY_ENABLED=true 并配置 peers)。"
        peer = client._config.peer_by_name(peer_name)
        if peer is None:
            return f"未找到 peer: {peer_name}。请在 a2a_gateway.peers 配置该 peer。"

        parts: list[dict[str, Any]] = []
        if message:
            parts.append({"text": message})

        if abs_file_path:
            file_part = self._build_local_file_part(abs_file_path, client)
            if isinstance(file_part, str):  # error message
                return file_part
            parts.append(file_part)
        elif file_uri:
            parts.append(self._build_uri_file_part(file_uri))
        else:
            return "必须提供 file_uri 或 abs_file_path 之一。"

        result = await client.send_message(peer, parts=parts, agent_id=agent_id)
        return self._format_result(result, peer_name)

    async def a2a_send_local_file(
        self,
        peer_name: str,
        abs_file_path: str,
        agent_id: str = "",
        message: str = "",
    ) -> str:
        """Send a local file to an A2A peer (basename as transport name)."""
        return await self.a2a_send_file(
            peer_name=peer_name, abs_file_path=abs_file_path,
            agent_id=agent_id, message=message,
        )

    async def a2x_list_agents(
        self,
        dataset: str = "",
        status: str = "online",
        include_all_datasets: bool = False,
    ) -> str:
        """List agents currently registered in the A2X registry (read-only).

        Opens a short-lived A2X registry client from the process config,
        queries the services, and returns a human-readable summary each
        agent's name / description / endpoint / status / id (and
        ``tunnelDeviceId`` when present). ``include_all_datasets`` enumerates
        every dataset instead of just one.
        """
        return await _a2x_list_agents_impl(dataset, status, include_all_datasets)

    async def a2a_send_message(
        self,
        agent_name: str,
        message: str,
        dataset: str = "",
    ) -> str:
        """Send a text message to an A2X-registered agent and return its reply.

        Resolves the target by name/id in the registry dataset, builds a
        :class:`PeerConfig` carrying the agent's endpoint + tunnel device id,
        and delegates to the process-wide outbound client's ``send_message``.
        Tunnel peers are forwarded through the relay; direct peers dial out.
        """
        return await _a2a_send_message_impl(agent_name, message, dataset)

    def _build_local_file_part(self, abs_file_path: str, client) -> dict[str, Any] | str:
        """Read a local file and build a FilePart dict (raw bytes, basename)."""
        path = Path(abs_file_path).expanduser()
        if not path.is_absolute():
            path = Path(os.path.abspath(str(path)))
        if not path.exists() or not path.is_file():
            return f"文件不存在或不是文件: {abs_file_path}"
        size = path.stat().st_size
        limit = client._config.security.max_file_size_bytes
        if size > limit:
            return f"文件过大({size} bytes)，上限 {limit} bytes。"
        allowed = client._config.security.allowed_mime_types
        mime = _guess_mime(str(path))
        if allowed and mime not in allowed:
            return f"文件类型 {mime} 不在允许列表内。"
        raw = path.read_bytes()
        # Transport name is always the basename (prevent model fabricating names).
        return {
            "raw": base64.b64encode(raw),  # A2A FilePart bytes
            "filename": path.name,
            "media_type": mime,
        }

    def _build_uri_file_part(self, file_uri: str) -> dict[str, Any]:
        """Build a URL FilePart for a publicly-reachable file."""
        from urllib.parse import urlparse  # noqa: PLC0415
        name = Path(urlparse(file_uri).path).name or "file"
        return {"url": file_uri, "filename": name, "media_type": _guess_mime(name)}

    def _format_result(self, result, peer_name: str) -> str:
        if result.ok:
            return result.text or f"已发送至 peer {peer_name}，远端未返回文本。"
        return f"发送至 {peer_name} 失败 (status={result.status_code}): {result.response.get('error', '')}"


# ── A2X registry query (read-only) ─────────────────────────────────────────


def _agent_field(agent: dict[str, Any], key: str, *aliases: str) -> str:
    """Read a string field from an agent dict, tolerating metadata nesting."""
    for name in (key, *aliases):
        value = str(agent.get(name) or "").strip()
        if value:
            return value
    metadata = agent.get("metadata")
    if isinstance(metadata, dict):
        for name in (key, *aliases):
            value = str(metadata.get(name) or "").strip()
            if value:
                return value
    return ""


def _format_a2x_agent(agent: dict[str, Any]) -> str:
    """Render one A2X service entry as a readable bullet."""
    name = _agent_field(agent, "name")
    if not name:
        # Blank agents encode the endpoint in the name; fall back to id so
        # the row is never empty.
        name = _agent_field(agent, "id", "service_id") or "(未命名)"
    desc = _agent_field(agent, "description")
    endpoint = _agent_field(agent, "endpoint", "url")
    st = _agent_field(agent, "status")
    sid = _agent_field(agent, "id", "service_id")
    tunnel = _agent_field(agent, "tunnelDeviceId")
    parts = [f"- {name}"]
    if desc and desc != name:
        parts.append(f"description={desc}")
    if endpoint:
        parts.append(f"endpoint={endpoint}")
    if st:
        parts.append(f"status={st}")
    if sid:
        parts.append(f"id={sid}")
    if tunnel:
        parts.append(f"tunnelDeviceId={tunnel}")
    return "  ".join(parts)


async def _a2x_list_agents_impl(
    dataset: str = "",
    status: str = "online",
    include_all_datasets: bool = False,
) -> str:
    """Query the A2X registry for registered agents and return a summary.

    Read-only: never mutates registry state. Late-imports the registry runtime
    + client so the tools module stays importable without a configured
    registry. A missing/non-distributed config is reported, not raised.
    """
    status = str(status or "online").strip().lower()
    valid = {"online", "busy", "offline", "all"}
    if status not in valid:
        status = "online"

    logger.info(
        "[a2x_list_agents] invoked dataset=%r status=%r include_all_datasets=%r",
        dataset, status, include_all_datasets,
    )

    try:
        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
            init_a2x_client,
            resolve_a2x_config,
        )
    except Exception as exc:  # import guard: keep the tool resilient
        logger.warning("[a2x_list_agents] registry module import failed: %s", exc)
        return f"A2X 注册中心模块不可用: {exc}"

    config_base = get_config()
    a2x_cfg = resolve_a2x_config(config_base)
    base_url = a2x_cfg.get("base_url") or ""
    configured_dataset = a2x_cfg.get("dataset") or ""
    logger.info(
        "[a2x_list_agents] resolved config base_url=%r dataset=%r role=%r distributed=%r",
        base_url, configured_dataset, a2x_cfg.get("role"), a2x_cfg.get("distributed_mode"),
    )

    client = None
    try:
        client, _ = await init_a2x_client(config_base)
    except Exception as exc:
        logger.warning("[a2x_list_agents] client init failed: %s", exc, exc_info=True)
        return f"A2X 注册中心客户端初始化失败(base_url={base_url}): {exc}"

    try:
        if include_all_datasets:
            datasets = await _list_all_datasets(client)
        else:
            target = str(dataset or "").strip() or configured_dataset
            if not target:
                logger.info("[a2x_list_agents] no dataset configured; returning hint")
                return (
                    "未指定 dataset 且 react.a2x_registry.dataset 未配置，"
                    "无法查询。可设 include_all_datasets=true 枚举全部数据集。"
                )
            datasets = [target]

        lines: list[str] = []
        total = 0
        for ds in datasets:
            filters: dict[str, Any] = {}
            if status != "all":
                filters["status"] = status
            agents = await client.list_agents(ds, **filters)
            logger.info(
                "[a2x_list_agents] query dataset=%r filters=%r -> %d agents",
                ds, filters, len(agents),
            )
            lines.append(f"[{ds}] ({len(agents)} 个{'，状态=' + status if status != 'all' else '全部状态'})")
            if not agents:
                lines.append("  (无)")
            for agent in agents:
                if isinstance(agent, dict):
                    lines.append("  " + _format_a2x_agent(agent))
                    total += 1
            lines.append("")  # blank line between datasets

        header = f"A2X 注册中心({base_url})共 {len(datasets)} 个数据集、{total} 个 agent"
        result = header + "\n" + "\n".join(lines).rstrip()
        logger.info("[a2x_list_agents] returning %d chars, %d agents total", len(result), total)
        return result
    except Exception as exc:
        logger.warning("[a2x_list_agents] query failed: %s", exc, exc_info=True)
        return f"A2X 注册中心查询失败: {exc}"
    finally:
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug("[a2x_list_agents] A2X client close failed: %s", exc)


async def _list_all_datasets(client: Any) -> list[str]:
    """Return all dataset names visible to the registry client.

    The A2X registry exposes a top-level ``GET /api/datasets`` listing; the
    client's transport is reused to hit it directly so we don't need a
    dedicated client method.
    """
    transport = getattr(client, "_transport", None)
    if transport is None:
        return []
    resp = await transport.request("GET", "/api/datasets")
    data = resp.json()
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    names.append(name)
    return names


# ── A2A send-message (text, by registry agent name) ───────────────────────


def _match_agents(agents: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Return agents whose name or id contains *target* (case-insensitive)."""
    needle = str(target or "").strip().lower()
    if not needle:
        return []
    matched: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        name = _agent_field(agent, "name").lower()
        sid = _agent_field(agent, "id", "service_id").lower()
        if needle in name or needle in sid or name in needle:
            matched.append(agent)
    return matched


async def _a2a_send_message_impl(
    agent_name: str,
    message: str,
    dataset: str = "",
) -> str:
    """Resolve an A2X-registered agent by name and send it a text message.

    Queries the dataset's online agents, matches by name/id (partial), builds
    a :class:`PeerConfig` (with ``tunnel_device_id`` when the agent carries
    one), and delegates to the process-wide outbound client. The outbound
    client routes tunnel peers through the relay and direct peers over TCP.
    """
    agent_name = str(agent_name or "").strip()
    message = str(message or "")
    if not agent_name:
        return "未提供 agent_name。"
    if not message:
        return "未提供 message。"

    from jiuwenswarm.agents.harness.common.a2a_gateway.runtime import get_outbound_client
    outbound = get_outbound_client()
    if outbound is None:
        return "A2A 出站未启用：未找到 outbound 客户端(请设置 A2A_GATEWAY_ENABLED=true)。"

    # Resolve the target agent from the A2X registry.
    target_ds, agents, a2x_client = await _query_dataset_online_agents(dataset)
    try:
        if not agents:
            return f"数据集 {target_ds} 里没有在线 agent。"
        matched = _match_agents(agents, agent_name)
        if not matched:
            avail = "、".join(_agent_field(a, "name") for a in agents if isinstance(a, dict))
            return f"未找到匹配 '{agent_name}' 的在线 agent。可用：{avail}"
        if len(matched) > 1:
            names = "、".join(_agent_field(a, "name") for a in matched)
            return f"'{agent_name}' 匹配到多个 agent：{names}。请用更精确的名称。"
        agent = matched[0]
    finally:
        if a2x_client is not None:
            try:
                await a2x_client.aclose()
            except Exception as exc:
                logger.debug("[a2a_send_message] A2X client close failed: %s", exc)

    # Build a PeerConfig from the resolved agent's card fields.
    from jiuwenswarm.agents.harness.common.a2a_gateway.config import PeerConfig
    endpoint = _agent_field(agent, "endpoint", "url")
    if not endpoint:
        return f"目标 agent {_agent_field(agent, 'name')} 缺少 endpoint，无法发送。"
    tunnel_device_id = _agent_field(agent, "tunnelDeviceId") or None
    peer = PeerConfig(
        name=_agent_field(agent, "name") or agent_name,
        agent_card_url=endpoint,
        tunnel_device_id=tunnel_device_id,
        # Registry-resolved peers carry no per-peer ``auth``; attach the
        # gateway's default peer token (a2a_gateway.security.peer_token) so
        # authenticated peers don't reject the request with HTTP 401.
        auth=outbound._config.default_peer_auth(),
    )
    logger.info(
        "[a2a_send_message] resolved target name=%s endpoint=%s tunnel=%s",
        peer.name, peer.agent_card_url, tunnel_device_id or "none",
    )

    try:
        result = await outbound.send_message(peer, text=message)
    except Exception as exc:
        logger.warning("[a2a_send_message] send_message failed: %s", exc, exc_info=True)
        return f"发送至 {peer.name} 失败: {exc}"

    if result.ok:
        return result.text or f"已发送至 {peer.name}，远端未返回文本。"
    return (
        f"发送至 {peer.name} 失败 (status={result.status_code}): "
        f"{result.response.get('error', '')}"
    )


async def _query_dataset_online_agents(dataset: str) -> tuple[str, list[dict[str, Any]], Any]:
    """Return (dataset, online_agents, a2x_client) for the given dataset.

    The caller owns closing ``a2x_client``. Falls back to the configured
    dataset when *dataset* is empty; returns an empty list when the registry
    is unreachable so the tool reports a clean message instead of crashing.
    """
    try:
        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
            init_a2x_client,
            resolve_a2x_config,
        )
    except Exception as exc:
        return dataset or "", [], None

    config_base = get_config()
    a2x_cfg = resolve_a2x_config(config_base)
    target_ds = str(dataset or "").strip() or a2x_cfg.get("dataset") or ""
    if not target_ds:
        return "", [], None

    client = None
    try:
        client, _ = await init_a2x_client(config_base)
        agents = await client.list_agents(target_ds, status="online")
        return target_ds, list(agents or []), client
    except Exception as exc:
        logger.warning("[a2a_send_message] registry query failed dataset=%s: %s", target_ds, exc)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        return target_ds, [], None
