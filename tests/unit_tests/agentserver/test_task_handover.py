# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for 「交接为跨设备任务」(session → relay task export).

The safety property this pins: a handover must never touch the task bridge —
``task.acquire`` through the bridge would subscribe it to the new task and make
every imported history entry (each a ``text`` without ``reply_to``) trigger an
agent turn. So ``handover_session`` uses the raw runtime seams only: create →
bare acquire → append each turn → release(input_required). All seams are
injected; nothing here touches a network, a relay, or an AgentServer.
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.agents.harness.team.tasks.errors import TaskRequestInvalidError
from jiuwenswarm.agents.harness.team.tasks.handover import (
    HANDOVER_MAX_MESSAGES,
    HANDOVER_TEXT_LIMIT,
    TOOL_ARGUMENTS_LIMIT,
    TOOL_RESULT_LIMIT,
    TRUNCATION_MARKER,
    _export_entries,
    _truncate,
    handover_session,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Recorder:
    """Callable that records every call's kwargs and returns canned results."""

    def __init__(self, result=None, error: Exception | None = None) -> None:  # noqa: ANN001
        self.calls: list[dict] = []
        self._result = result if result is not None else {}
        self._error = error

    async def __call__(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        result = self._result
        if callable(result):
            return result(kwargs)
        return result


def _history_records() -> list[dict]:
    return [
        {"role": "user", "content": "第一问"},
        {"role": "tool", "content": "内部工具调用，不该导出"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_export_entries_filters_to_text_turns() -> None:
    kept = _export_entries(_history_records())
    assert kept == [
        {"type": "text", "role": "user", "text": "第一问"},
        {"type": "text", "role": "assistant", "text": "第一答"},
        {"type": "text", "role": "user", "text": "第二问"},
    ]
    assert _export_entries("not-a-list") == []
    assert _export_entries([{"role": "user"}]) == []


def _tool_history_records() -> list[dict]:
    """一次真实团队会话的缩影：提问 → 思考+工具调用 → 结果 → 被打断的
    子任务 → 最终答复。"""
    return [
        {"role": "user", "content": "帮我调研下 llm 推理相关技术"},
        {
            "role": "assistant",
            "event_type": "chat.tool_call",
            "content": "",
            "reasoning_content": "先查一下生态。",
            "tool_call": {
                "name": "search_tools",
                "arguments": '{"query": "llm inference"}',
                "tool_call_id": "call-1",
                "display_name": "工具检索",
            },
        },
        {
            "role": "assistant",
            "event_type": "chat.tool_result",
            "content": "",
            "tool_name": "search_tools",
            "tool_call_id": "call-1",
            "result": "找到 3 个相关工具。",
        },
        {
            "role": "assistant",
            "event_type": "chat.tool_call",
            "content": "",
            "tool_call": {
                "name": "task_tool",
                "arguments": '{"prompt": "调研推理框架"}',
                "tool_call_id": "call-2",
            },
        },
        {
            # 交接时自动打断留下的记录：结果收在 tool_result 子字典里。
            "role": "assistant",
            "event_type": "chat.tool_result",
            "content": "[Interrupted] Tool execution cancelled by user.",
            "tool_result": {
                "tool_name": "task_tool",
                "tool_call_id": "call-2",
                "result": "Tool execution cancelled by user.",
                "status": "error",
            },
        },
        {"role": "assistant", "event_type": "chat.final", "content": "已加载工具，将派出子代理调研。"},
    ]


def test_export_entries_pairs_tool_calls_with_results() -> None:
    entries = _export_entries(_tool_history_records())
    assert entries == [
        {"type": "text", "role": "user", "text": "帮我调研下 llm 推理相关技术"},
        {
            "type": "tool",
            "tool_name": "search_tools",
            "arguments": '{"query": "llm inference"}',
            "thinking": "先查一下生态。",
            "result": "找到 3 个相关工具。",
            "status": None,
            "file_paths": [],
        },
        {
            # 被打断的子任务也是历史的一部分：调用与结果配对，status=error。
            "type": "tool",
            "tool_name": "task_tool",
            "arguments": '{"prompt": "调研推理框架"}',
            "thinking": None,
            "result": "Tool execution cancelled by user.",
            "status": "error",
            "file_paths": [],
        },
        {"type": "text", "role": "assistant", "text": "已加载工具，将派出子代理调研。"},
    ]


def test_export_entries_extracts_file_paths_from_file_tools() -> None:
    """文件工具的路径在截断前解析：write_file 参数直给路径；send_file_to_user
    的 abs_file_path_list 兼容单串 / JSON 数组串。其它工具不受影响。"""
    entries = _export_entries(
        [
            {
                "role": "assistant",
                "event_type": "chat.tool_call",
                "content": "",
                "tool_call": {
                    "name": "write_file",
                    # 参数长到必然被截断——路径必须在此之前拿到。
                    "arguments": json.dumps(
                        {
                            "file_path": "C:\\proj\\gomoku.html",
                            "content": "字" * (TOOL_ARGUMENTS_LIMIT + 10),
                        }
                    ),
                    "tool_call_id": "w1",
                },
            },
            {
                "role": "assistant",
                "event_type": "chat.tool_call",
                "content": "",
                "tool_call": {
                    "name": "send_file_to_user",
                    "arguments": json.dumps(
                        {"abs_file_path_list": '["C:\\\\proj\\\\gomoku.html", "C:\\\\proj\\\\a.png"]'}
                    ),
                    "tool_call_id": "s1",
                },
            },
        ]
    )
    assert entries[0]["file_paths"] == ["C:\\proj\\gomoku.html"]
    # 上面内层还是 JSON 字符串（真实 history 的形态），也要展开。
    assert entries[1]["file_paths"] == ["C:\\proj\\gomoku.html", "C:\\proj\\a.png"]
    # 参数本身照旧截断。
    assert entries[0]["arguments"].endswith(TRUNCATION_MARKER)
    # 非文件工具/坏参数不报错。
    assert _export_entries(
        [
            {
                "role": "assistant",
                "event_type": "chat.tool_call",
                "content": "",
                "tool_call": {"name": "write_file", "arguments": "not-json", "tool_call_id": "x"},
            }
        ]
    )[0]["file_paths"] == []


def test_export_entries_keeps_orphan_results_and_truncates() -> None:
    long_result = "果" * (TOOL_RESULT_LIMIT + 10)
    entries = _export_entries(
        [
            # 没有对应调用的孤儿结果（max_messages 截窗后常见）单独成条。
            {
                "role": "assistant",
                "event_type": "chat.tool_result",
                "content": "",
                "tool_name": "read_file",
                "tool_call_id": "gone",
                "result": long_result,
            },
            # 无名无果的工具结果丢弃。
            {
                "role": "assistant",
                "event_type": "chat.tool_result",
                "content": "",
            },
            # 只有 result、没有 tool_name 的孤儿：标 unknown 而不是丢内容。
            {
                "role": "assistant",
                "event_type": "chat.tool_result",
                "content": "",
                "tool_call_id": "gone-2",
                "result": "裸结果",
            },
        ]
    )
    assert [entry["tool_name"] for entry in entries] == ["read_file", "unknown"]
    assert entries[0]["result"] == "果" * TOOL_RESULT_LIMIT + TRUNCATION_MARKER
    assert entries[0]["status"] is None
    assert entries[1]["result"] == "裸结果"


def test_truncate_marks_long_text() -> None:
    long = "字" * (HANDOVER_TEXT_LIMIT + 5)
    out = _truncate(long)
    assert out == "字" * HANDOVER_TEXT_LIMIT + TRUNCATION_MARKER
    assert _truncate("短文本") == "短文本"


# ── the export flow ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_handover_exports_turns_and_releases() -> None:
    create = _Recorder({"task_id": 9})
    acquire = _Recorder({"task_id": 9, "fence": 4})
    append = _Recorder(lambda kwargs: {"task_id": 9, "seq": 100 + len(append.calls)})
    release = _Recorder({"task_id": 9, "state": "input_required", "released": True})

    result = await handover_session(
        session_id="sess-1",
        title="我的会话",
        record_loader=lambda session_id: _history_records(),
        creator=create,
        acquirer=acquire,
        appender=append,
        releaser=release,
    )

    assert result == {"task": {"task_id": 9}}
    assert create.calls == [
        {
            "title": "我的会话",
            "payload": {"source": "web_session", "session_id": "sess-1"},
            "config_base": None,
        }
    ]
    # 裸 acquire（force），绝不经过 bridge.take_task。
    assert acquire.calls == [{"task_id": 9, "ttl_seconds": None, "mode": "force", "config_base": None}]

    assert len(append.calls) == 3
    assert append.calls[0]["fence"] == 4
    assert append.calls[0]["kind"] == "text"
    assert append.calls[0]["reply_to"] is None
    assert append.calls[0]["client_msg_id"] == "handover-9-0"
    assert append.calls[1]["client_msg_id"] == "handover-9-1"
    # 角色保留在 payload 里，接续方据此区分导入的对话两侧。
    assert [call["payload"]["role"] for call in append.calls] == ["user", "assistant", "user"]
    assert [call["payload"]["text"] for call in append.calls] == ["第一问", "第一答", "第二问"]

    assert len(release.calls) == 1
    assert release.calls[0]["fence"] == 4
    assert release.calls[0]["next_state"] == "input_required"
    assert release.calls[0]["checkpoint"] == {"last_seq": 103, "source_session": "sess-1"}
    assert release.calls[0]["checkpoint_seq"] == 103


@pytest.mark.anyio
async def test_handover_exports_tool_events_as_tool_result_messages() -> None:
    """中间过程可见性：工具调用/结果以 tool_result kind 落进日志，payload
    带结构化字段 + 预渲染 text 摘要（旧读者把它当普通文本）。"""
    append = _Recorder(lambda kwargs: {"task_id": 5, "seq": 10 + len(append.calls)})

    await handover_session(
        session_id="sess-tool",
        record_loader=lambda session_id: _tool_history_records(),
        creator=_Recorder({"task_id": 5}),
        acquirer=_Recorder({"task_id": 5, "fence": 1}),
        appender=append,
        releaser=_Recorder({}),
    )

    assert [call["kind"] for call in append.calls] == [
        "text",
        "tool_result",
        "tool_result",
        "text",
    ]
    tool_payload = append.calls[1]["payload"]
    assert tool_payload["tool_name"] == "search_tools"
    assert tool_payload["arguments"] == '{"query": "llm inference"}'
    assert tool_payload["thinking"] == "先查一下生态。"
    assert tool_payload["result"] == "找到 3 个相关工具。"
    assert tool_payload["status"] is None
    # 预渲染摘要：message_text()/旧面板都能读的兜底文本。
    assert tool_payload["text"] == "[工具 search_tools] 找到 3 个相关工具。"
    # role=assistant：导入的工具活动属于 agent 一侧。
    assert tool_payload["role"] == "assistant"
    # 被打断的子任务同样导出，status=error。
    interrupted = append.calls[2]["payload"]
    assert interrupted["tool_name"] == "task_tool"
    assert interrupted["status"] == "error"
    # 对话两侧不受影响。
    assert append.calls[0]["payload"]["text"] == "帮我调研下 llm 推理相关技术"
    assert append.calls[3]["payload"]["text"] == "已加载工具，将派出子代理调研。"


@pytest.mark.anyio
async def test_handover_title_falls_back_to_session_id_and_caps_length() -> None:
    create = _Recorder({"task_id": 1})
    acquire = _Recorder({"task_id": 1, "fence": 1})
    append = _Recorder({"task_id": 1, "seq": 1})
    release = _Recorder({})

    await handover_session(
        session_id="sess-2",
        title="",
        record_loader=lambda session_id: [{"role": "user", "content": "问"}],
        creator=create,
        acquirer=acquire,
        appender=append,
        releaser=release,
    )
    assert create.calls[0]["title"] == "sess-2"

    create2 = _Recorder({"task_id": 2})
    await handover_session(
        session_id="sess-3",
        title="长" * 500,
        record_loader=lambda session_id: [{"role": "user", "content": "问"}],
        creator=create2,
        acquirer=acquire,
        appender=append,
        releaser=release,
    )
    assert len(create2.calls[0]["title"]) == 200


@pytest.mark.anyio
async def test_handover_takes_only_the_last_n_turns() -> None:
    append = _Recorder({"task_id": 1, "seq": 1})
    records = [
        {"role": "user", "content": f"问题{i}"} for i in range(HANDOVER_MAX_MESSAGES + 3)
    ]

    await handover_session(
        session_id="sess-4",
        record_loader=lambda session_id: records,
        creator=_Recorder({"task_id": 1}),
        acquirer=_Recorder({"task_id": 1, "fence": 1}),
        appender=append,
        releaser=_Recorder({}),
    )
    assert len(append.calls) == HANDOVER_MAX_MESSAGES
    # 保留的是最近的 N 条。
    assert append.calls[0]["payload"]["text"] == f"问题3"


@pytest.mark.anyio
async def test_handover_without_restorable_history_is_rejected() -> None:
    with pytest.raises(TaskRequestInvalidError):
        await handover_session(
            session_id="sess-5",
            record_loader=lambda session_id: [{"role": "tool", "content": "只有工具记录"}],
            creator=_Recorder({"task_id": 1}),
            acquirer=_Recorder({"task_id": 1, "fence": 1}),
            appender=_Recorder({}),
            releaser=_Recorder({}),
        )


@pytest.mark.anyio
async def test_handover_releases_even_when_an_append_fails() -> None:
    """中途失败也要 release：input_required 落库的部分仍可接管，胜过悬挂租约。"""
    release = _Recorder({})

    class _FailAfterFirst:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, **kwargs):  # noqa: ANN003
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("relay hiccup")
            return {"task_id": 1, "seq": 7}

    with pytest.raises(RuntimeError):
        await handover_session(
            session_id="sess-6",
            record_loader=lambda session_id: [
                {"role": "user", "content": "问1"},
                {"role": "assistant", "content": "答"},
                {"role": "user", "content": "问2"},
            ],
            creator=_Recorder({"task_id": 1}),
            acquirer=_Recorder({"task_id": 1, "fence": 2}),
            appender=_FailAfterFirst(),
            releaser=release,
        )

    assert len(release.calls) == 1
    assert release.calls[0]["next_state"] == "input_required"
    assert release.calls[0]["checkpoint"]["last_seq"] == 7


# ── artifact archiving（文件经 registry 中转）────────────────────────────────


def _gomoku_records() -> list[dict]:
    """生成文件的会话缩影：write_file 写出 → send_file_to_user 投递。"""
    return [
        {"role": "user", "content": "帮我实现一个五子棋的HTML游戏"},
        {
            "role": "assistant",
            "event_type": "chat.tool_call",
            "content": "",
            "tool_call": {
                "name": "write_file",
                "arguments": json.dumps(
                    {"file_path": "C:\\buddy\\gomoku.html", "content": "<html>…</html>"}
                ),
                "tool_call_id": "w1",
            },
        },
        {
            "role": "assistant",
            "event_type": "chat.tool_result",
            "content": "",
            "tool_name": "write_file",
            "tool_call_id": "w1",
            "result": "success=True data={'file_path': 'C:\\buddy\\gomoku.html', 'bytes_written': 23753}",
        },
        {
            "role": "assistant",
            "event_type": "chat.tool_call",
            "content": "",
            "tool_call": {
                "name": "send_file_to_user",
                "arguments": json.dumps({"abs_file_path_list": "C:\\buddy\\gomoku.html"}),
                "tool_call_id": "s1",
            },
        },
        {
            "role": "assistant",
            "event_type": "chat.tool_result",
            "content": "",
            "tool_name": "send_file_to_user",
            "tool_call_id": "s1",
            "result": "成功发送 1 个文件",
        },
        {"role": "assistant", "event_type": "chat.final", "content": "五子棋已完成，文件已发送。"},
    ]


_GOMOKU_REF = {
    "name": "gomoku.html",
    "size": 23753,
    "media_type": "text/html",
    "sha256": "a" * 64,
    "transfer_id": "f" * 32,
    "download_url": "http://relay.test/api/artifact-relay/uri/fff/uri-token",
    "expires_at": "2026-10-14T00:00:00Z",
}


@pytest.mark.anyio
async def test_handover_attaches_artifact_refs_to_file_tool_entries() -> None:
    """交接上传成功时：write_file 与 send_file_to_user 的条目都带 artifacts
    数组 + text 里的 📎 摘要行；无关条目不受影响。"""
    append = _Recorder(lambda kwargs: {"task_id": 6, "seq": len(append.calls)})

    async def uploader(paths, *, config_base=None):  # noqa: ANN001
        assert paths == ["C:\\buddy\\gomoku.html"]  # 两个工具引用同一文件，只传一次
        return {"C:\\buddy\\gomoku.html": dict(_GOMOKU_REF)}

    await handover_session(
        session_id="sess-gomoku",
        record_loader=lambda session_id: _gomoku_records(),
        creator=_Recorder({"task_id": 6}),
        acquirer=_Recorder({"task_id": 6, "fence": 1}),
        appender=append,
        releaser=_Recorder({}),
        artifact_uploader=uploader,
    )

    by_tool = {call["payload"].get("tool_name"): call["payload"] for call in append.calls}
    write_payload = by_tool["write_file"]
    assert write_payload["artifacts"] == [_GOMOKU_REF]
    assert "📎 已存档文件" in write_payload["text"]
    assert _GOMOKU_REF["download_url"] in write_payload["text"]
    # 同一文件在投递工具条目上也可见（引用同一个存档）。
    assert by_tool["send_file_to_user"]["artifacts"] == [_GOMOKU_REF]
    # 文本条目与摘要原样。
    assert append.calls[0]["payload"]["text"] == "帮我实现一个五子棋的HTML游戏"
    assert by_tool["write_file"]["result"].startswith("success=True")


@pytest.mark.anyio
async def test_handover_survives_artifact_upload_failure() -> None:
    """存档是增强不是前提：上传炸了，交接照常完成、payload 不带 artifacts。"""
    append = _Recorder(lambda kwargs: {"task_id": 6, "seq": len(append.calls)})

    async def uploader(paths, *, config_base=None):  # noqa: ANN001
        raise RuntimeError("artifact relay down")

    await handover_session(
        session_id="sess-gomoku-2",
        record_loader=lambda session_id: _gomoku_records(),
        creator=_Recorder({"task_id": 6}),
        acquirer=_Recorder({"task_id": 6, "fence": 1}),
        appender=append,
        releaser=_Recorder({}),
        artifact_uploader=uploader,
    )

    assert len(append.calls) == 4  # user + write_file + send_file_to_user + final
    for call in append.calls:
        assert "artifacts" not in call["payload"]
    by_tool = {call["payload"].get("tool_name"): call["payload"] for call in append.calls}
    assert "📎" not in by_tool["write_file"]["text"]
