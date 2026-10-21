# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session handover: export a local conversation to the relay as a task.

「交接为跨设备任务」的服务端原子实现：读取本机会话的 history.jsonl，把
user/assistant 文本轮次逐条写进一个新建的 relay 任务，**连同中间过程**——
工具调用（名称/参数/结果/思考摘要）以 ``tool_result`` kind 落在同一个日志
里——然后以 ``input_required`` 释放。任何设备（含手机）随后都能在任务列
表里接管继续，且看到的不再只是对话骨架：原会话「查了什么、调了什么工具、
结果是什么」全部可见。

为什么不走前端编排的 ``task.acquire`` + ``task.message.send``：

* ``task.acquire`` 会路由到 ``bridge.take_task``，本机 bridge 从此持有租约
  并订阅该任务——而导入的历史每条都是不带 ``reply_to`` 的 text 消息，会让
  bridge 对每条历史各跑一轮 agent 回合。所以这里用**裸** ``runtime.acquire``，
  全程不触碰 bridge。
* 长会话的历史在服务端磁盘上，逐条从浏览器搬运要经过网关 ws 多帧传输；
  服务端逐条调用 relay 是有界的 HTTP，一条失败就是一个明确的错误。

工具记录的取舍：``tool_result`` kind 的消息**不会**触发 bridge 回合（``should_trigger_turn``
只认无 ``reply_to`` 的 text），payload 里带一份预渲染的 ``text`` 摘要，所以
未升级的旧面板/bridge 提示词也能把它当普通文本读。工具正文仍走截断——导出的
是「可见性」；但会话**产出的文件**是另一回事：跨设备接续没有字节就什么都
续不了，所以它们经 relay 的 artifact-relay 上传（``artifacts.py``），引用附在
对应工具条目的 payload 里，接手设备下载回本机工作区。

每个 IO 缝都可注入，与 ``bridge.py`` 同一测试姿态。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from .artifacts import archive_summary, extract_file_paths
from .errors import TaskRequestInvalidError

logger = logging.getLogger(__name__)

HANDOVER_MAX_MESSAGES = 100
"""最多导出的日志条数（对话轮次 + 工具条目合计）：取最近的 N 条，更早的
上下文由接续方自行总结。"""

HANDOVER_TEXT_LIMIT = 60_000
"""单条消息正文的字符上限。

relay 按 ``MAX_MESSAGE_BYTES``（64KB）限制整个 payload 的 JSON 体积，中文
按 UTF-8 每字符最多 3 字节，60k 字符留出 payload 外壳的余量；超限截断并
标记，而不是让整条 append 失败。
"""

TOOL_RESULT_LIMIT = 4_000
"""单条工具结果的字符上限。工具结果是给人看的「可见性」，不是字节搬迁；
超限截断。"""

TOOL_ARGUMENTS_LIMIT = 1_000
"""工具调用参数的字符上限（参数常常是长 prompt，截断即可）。"""

TOOL_THINKING_LIMIT = 500
"""工具调用附带思考摘要的字符上限。"""

TOOL_SUMMARY_LIMIT = 200
"""payload.text 预渲染摘要里结果预览的长度。"""

TRUNCATION_MARKER = "…[已截断]"


def _truncate(text: str, limit: int = HANDOVER_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


def _tool_summary(name: str, result: str) -> str:
    """Pre-render the ``text`` summary every generic reader falls back to."""
    preview = " ".join(result.split())
    if len(preview) > TOOL_SUMMARY_LIMIT:
        preview = preview[:TOOL_SUMMARY_LIMIT] + TRUNCATION_MARKER
    if not name:
        name = "unknown"
    return f"[工具 {name}] {preview}" if preview else f"[工具 {name}]"


def build_tool_entry_payload(
    *,
    tool_name: str,
    arguments: str | None,
    thinking: str | None,
    result: str | None,
    status: str | None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The canonical ``tool_result`` payload: one tool call + its result.

    会话交接（批量导入）与 bridge 的实时回合（逐条落库）写的是同一种条目，
    格式只在这一处定义：``payload.text`` 预渲染摘要让旧面板/bridge 提示词当
    普通文本读，``artifacts`` 数组供升级过的面板渲染下载按钮、供接手设备
    ``collect_artifact_refs`` 物化文件。字段正文在这里统一截断——调用方预先
    截过也无妨（幂等）。
    """
    text = _tool_summary(tool_name, result or "")
    if refs:
        summary = archive_summary(refs)
        text = f"{text}\n{summary}" if text else summary
    payload: dict[str, Any] = {
        "role": "assistant",
        "text": text,
        "tool_name": tool_name,
        "arguments": _truncate(arguments, TOOL_ARGUMENTS_LIMIT) if arguments else None,
        "result": _truncate(result, TOOL_RESULT_LIMIT) if result else None,
        "thinking": _truncate(thinking, TOOL_THINKING_LIMIT) if thinking else None,
        "status": status,
    }
    if refs:
        payload["artifacts"] = refs
    return payload


def _export_entries(records: Any) -> list[dict[str, Any]]:
    """Turn history records into exportable log entries.

    Three record families come out of ``history.jsonl``:

    * 对话文本：``role=user`` 的记录，与 ``role=assistant`` 且
      ``event_type`` 为空或 ``chat.final`` 的记录（旧文件没有 event_type，
      只有 content；被打断的工具结果 content 非空但 event_type 是
      ``chat.tool_result``，绝不冒充回复文本）。
    * ``chat.tool_call``：``tool_call`` 子字典（name/arguments/tool_call_id）
      + 顶层 ``reasoning_content`` 思考摘要。
    * ``chat.tool_result``：正常结果是顶层 ``tool_name``/``tool_call_id``/
      ``result``；被打断的结果收在 ``tool_result`` 子字典里（status=error）。

    调用与结果按 ``tool_call_id`` 配对成**一条**工具条目，位置留在调用发生
    处（保持时序）；没有对应调用的孤儿结果单独成条。其余记录（role=tool
    等内部实现细节）仍然不导出。
    """
    if not isinstance(records, list):
        return []
    entries: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        role = record.get("role")
        event_type = str(record.get("event_type") or "")

        if event_type == "chat.tool_call":
            call = record.get("tool_call")
            if not isinstance(call, Mapping):
                continue
            name = str(call.get("name") or "").strip()
            if not name:
                continue
            raw_arguments = str(call.get("arguments") or "")
            entry: dict[str, Any] = {
                "type": "tool",
                "tool_name": name,
                "arguments": _truncate(raw_arguments, TOOL_ARGUMENTS_LIMIT),
                "thinking": _truncate(
                    str(record.get("reasoning_content") or "").strip(), TOOL_THINKING_LIMIT
                )
                or None,
                "result": None,
                "status": None,
                # 文件路径要在截断前从原始参数里解析——截断后的 JSON 大概率
                # 已经不完整。留在条目上供 handover_session 上传存档。
                "file_paths": extract_file_paths(name, raw_arguments),
            }
            call_id = str(call.get("tool_call_id") or "")
            if call_id:
                pending[call_id] = entry
            entries.append(entry)
            continue

        if event_type == "chat.tool_result":
            nested = record.get("tool_result")
            if isinstance(nested, Mapping):
                name = str(nested.get("tool_name") or "")
                call_id = str(nested.get("tool_call_id") or "")
                result = str(nested.get("result") or "")
                status = str(nested.get("status") or "") or None
            else:
                name = str(record.get("tool_name") or "")
                call_id = str(record.get("tool_call_id") or "")
                result = str(record.get("result") or record.get("content") or "")
                status = None
            target = pending.pop(call_id, None) if call_id else None
            if target is not None:
                target["result"] = _truncate(result, TOOL_RESULT_LIMIT)
                target["status"] = status
            else:
                if not name and not result.strip():
                    continue
                entries.append(
                    {
                        "type": "tool",
                        "tool_name": name or "unknown",
                        "arguments": None,
                        "thinking": None,
                        "result": _truncate(result, TOOL_RESULT_LIMIT),
                        "status": status,
                        "file_paths": [],
                    }
                )
            continue

        content = record.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            entries.append({"type": "text", "role": "user", "text": content})
        elif role == "assistant" and event_type in ("", "chat.final"):
            entries.append({"type": "text", "role": "assistant", "text": content})
    return entries


async def _archive_session_files(
    entries: list[dict[str, Any]],
    *,
    config_base: Mapping[str, Any] | None,
    artifact_uploader: Callable[..., Any] | None,
) -> dict[str, dict[str, Any]]:
    """Upload every file the exported entries reference; ``{path: ref}``.

    Injecting ``artifact_uploader``（tests 用）覆盖默认的
    ``artifacts.upload_session_artifacts``；未配置 create token 时它返回空
    map，交接退化成纯日志导出。任何异常都被吞掉记日志——存档失败不能让
    交接失败。
    """
    paths: list[str] = []
    for entry in entries:
        if entry.get("type") != "tool":
            continue
        for path in entry.get("file_paths") or ():
            # write_file 与 send_file_to_user 常引用同一文件；上传一次、
            # 两条目都挂同一个引用。注入的 uploader 拿到的也是去重后的清单。
            if path and path not in paths:
                paths.append(path)
    if not paths:
        return {}
    if artifact_uploader is None:
        from .artifacts import upload_session_artifacts as artifact_uploader
    try:
        refs = await artifact_uploader(paths, config_base=config_base)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：增强不设前提
        logger.warning(
            "[TaskHandover] artifact upload failed, continuing without files: %s",
            exc,
        )
        return {}
    return refs if isinstance(refs, Mapping) else {}


async def handover_session(
    *,
    session_id: str,
    title: str = "",
    max_messages: int = HANDOVER_MAX_MESSAGES,
    config_base: Mapping[str, Any] | None = None,
    record_loader: Callable[[str], Any] | None = None,
    creator: Callable[..., Any] | None = None,
    acquirer: Callable[..., Any] | None = None,
    appender: Callable[..., Any] | None = None,
    releaser: Callable[..., Any] | None = None,
    artifact_uploader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Export one local session to the relay. Returns ``{"task": ...}``.

    绝不经 ``bridge.take_task``（见模块 docstring）；裸 acquire 拿到的 fence
    只活在本次导出内，release 后即作废。

    会话产出的文件（write_file/send_file_to_user 引用到的）经 artifact-relay
    上传，引用附在对应工具条目的 payload（``artifacts`` + ``text`` 里的 📎
    行）。上传是增强不是前提：未配置或全部失败时交接照常完成，只是没有文件。
    """
    if record_loader is not None:
        records = record_loader(session_id)
    else:
        from jiuwenswarm.server.runtime.session.session_history import load_history_records

        records = load_history_records(session_id)

    entries = _export_entries(records)[-max_messages:]
    if not entries:
        raise TaskRequestInvalidError("会话没有可交接的历史记录")

    if creator is not None:
        create = creator
    else:
        from .runtime import create_task as create
    if acquirer is not None:
        acquire = acquirer
    else:
        from .runtime import acquire as acquire
    if appender is not None:
        append = appender
    else:
        from .runtime import append_message as append
    if releaser is not None:
        release = releaser
    else:
        from .runtime import release as release

    refs_by_path = await _archive_session_files(
        entries, config_base=config_base, artifact_uploader=artifact_uploader
    )

    task = await create(
        title=(title or session_id)[:200],
        payload={"source": "web_session", "session_id": session_id},
        config_base=config_base,
    )
    task_id = task.get("task_id")

    acquired = await acquire(task_id=task_id, ttl_seconds=None, mode="force", config_base=config_base)
    fence = int(acquired.get("fence") or 0)

    last_seq = 0
    try:
        for index, entry in enumerate(entries):
            if entry["type"] == "tool":
                # 工具条目走 tool_result kind：不触发 bridge 回合（should_trigger_turn
                # 只认 text），payload.text 预渲染摘要让旧读者也能当文本读。
                refs = [
                    refs_by_path[path]
                    for path in entry.get("file_paths") or ()
                    if path in refs_by_path
                ]
                payload = build_tool_entry_payload(
                    tool_name=entry["tool_name"],
                    arguments=entry["arguments"],
                    thinking=entry["thinking"],
                    result=entry["result"],
                    status=entry["status"],
                    refs=refs,
                )
                kind = "tool_result"
            else:
                payload = {"role": entry["role"], "text": _truncate(entry["text"])}
                kind = "text"
            posted = await append(
                task_id=task_id,
                fence=fence,
                payload=payload,
                kind=kind,
                client_msg_id=f"handover-{task_id}-{index}",
                reply_to=None,
                config_base=config_base,
            )
            if isinstance(posted, Mapping):
                try:
                    last_seq = max(last_seq, int(posted.get("seq") or 0))
                except (TypeError, ValueError):
                    pass
    finally:
        # 无论中途哪条失败都释放：任务以 input_required 落在 relay 上，已写入
        # 的部分仍然可接管，比留一个 working 状态的悬挂租约（等 TTL 过期）
        # 更接近「交接」的语义。
        await release(
            task_id=task_id,
            fence=fence,
            next_state="input_required",
            checkpoint={"last_seq": last_seq, "source_session": session_id},
            checkpoint_seq=last_seq,
            config_base=config_base,
        )

    logger.info(
        "[TaskHandover] session handed over: session=%s task_id=%s entries=%s",
        session_id,
        task_id,
        len(entries),
    )
    return {"task": task}
