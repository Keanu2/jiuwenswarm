# JiuwenSwarm A2A 跨设备改动说明（Mac 对照移植）

本文记录 2026-09 在 **JiuwenSwarm-Windows** 与 **JiuwenSwarm-HW-PC1** 上落地的传文件稳定性改动，方便另一台 Mac 按同样思路改源码和配置。

官方 A2A 通道接入见 [A2A.md](A2A.md)。**冲突时以源码为准。**

Mac 默认 `service_id` 已在源码里写成 `JiuwenSwarm-Mac`（`sys.platform == "darwin"`）。沙箱目录建议用桌面 `~/Desktop/JiuwenSwarm`。

---

## 1. 先弄清：慢和「发不出去」多半不是隧道

现象（照片、PPT、「在吗」要一两分钟甚至 300s 超时）排查后：

| 看起来像 | 实际原因 |
|---------|---------|
| 链路卡死 | A2A `a2a.send` **同步等待对端整轮 Agent 结束** |
| 不会发文件 | 模型调用了 `send_file_to_user`（网页/IM 投递），不是设备间工具 |
| 不知道发给谁 | 入站信封**没有**来源 `service_id`；只写「发给我」会在注册中心十几个设备里猜 |
| 手机把 HEIC 转 JPG | OpenClaw 当网页预览处理；A2A 可以原样发 HEIC |
| `tool_search` 再发，多一轮 20–40s | `a2a_send_message` 被渐进式工具做成 **deferred**；`send_file_to_user` 反而是 direct |
| 对端「在吗」又等两分钟 | 入站已经是 `a2a` 渠道，又嵌套 `a2a_send_message` 回去 |

JiuwenSwarm 侧设备间传文件的 LLM 工具只有：

```text
a2a_send_message(service_id=..., text=..., file_paths=[...])
```

它走 sidecar 的 `a2a.send`，把文件做成 FilePart。没有名为 `a2a_send_local_file` 的 JiuwenSwarm 工具。

OpenClaw（手机 **HW-Phone1**）相反：

```text
本机没有 a2a_send_message / file_paths / send_file_to_user
实际接口：a2a_send_local_file（等同 a2a_send_file）
原文件、不转格式；service_id 按注册中心逐字匹配
```

所以：**本机发文件用 Jiuwen 工具；请手机发回来时，正文要写手机的工具名。**

---

## 2. 改动文件清单（相对 `jiuwenswarm/` 包根）

仓库里 Python 包路径是 `jiuwenswarm/jiuwenswarm/...`（外层目录 + 包名）。下表用包内路径。

| 文件 | 改什么 |
|------|--------|
| `agents/harness/common/prompt/prompt_builder.py` | A2A 专段提示词；`send_file_to_user` 限定 web/IM |
| `agents/harness/common/rails/response_prompt_rail.py` | 每轮注入 A2A 段，带上本机 `JIUWENSWARM_A2A_SERVICE_ID` |
| `agents/harness/common/tools/a2a_registry_tools.py` | `ToolExposure.DIRECT`、50MB、工具卡文案、注册中心短缓存 |
| `agents/harness/common/tools/send_file_to_user.py` | 描述里禁止用于 A2A 设备 |
| `gateway/channel_manager/protocol/a2a/a2a_connect.py` | 50MB 收文件；纯投递自动 ACK，跳过接收端模型 |
| `common/file_sandbox.py` | 入站落到桌面沙箱 `persist_received_file`（Windows 源码已有） |
| `server/runtime/agent_adapter/interface_deep.py` | **不要**依赖 `progressive_tool_always_visible_tools`（框架忽略） |
| `~/.jiuwenswarm/config/.env` | 本机 ID、沙箱路径、50MB |
| `~/.jiuwenswarm/a2a-sidecar.json` | `maxInlineFileSizeBytes` = 50MB |
| `~/.jiuwenswarm/agent/workspace/memory/MEMORY.md` | 本机规则，重启后仍生效 |

测试（改完可跑，建议 `--no-cov`，否则 coverage 可能在 Windows 上卡住）：

- `tests/unit_tests/agentserver/test_a2a_transfer_prompt.py`
- `tests/unit_tests/agentserver/test_a2a_registry_tools.py`
- `tests/unit_tests/channel/test_a2a_channel.py`

```bash
python -m pytest tests/unit_tests/agentserver/test_a2a_transfer_prompt.py \
  tests/unit_tests/agentserver/test_a2a_registry_tools.py \
  tests/unit_tests/channel/test_a2a_channel.py \
  --no-cov --override-ini="addopts="
```

改完必须**重启** JiuwenSwarm + A2A sidecar，提示词和 `ToolExposure` 才进新会话。

---

## 3. 源码怎么改

### 3.1 提示词：A2A 专段（核心）

`prompt_builder.py`：

- `PromptPriority.A2A = 66`
- `LocalSectionName.A2A = "a2a_transfer"`
- `_a2a_transfer_prompt(language, self_service_id)` 中英两份
- `_output_prompt` 里：`send_file_to_user` 只给当前网页/IM 的人；A2A 用 `a2a_send_message` + `file_paths`

Mac 上 `self_service_id` 应为 `JiuwenSwarm-Mac`（由环境变量注入，见 3.2）。正文规则：

1. `a2a_send_message` / `a2a_registry_list` 是 **direct**，禁止 `tool_search`。
2. **当前渠道已经是 `a2a`**：纯文字（「在吗」）直接最终回复，禁止再 `a2a_send_message`（嵌套同步等待会拖到一两分钟）。
3. **本机发文件**：只 `a2a_send_message` + `file_paths`，不转 HEIC/JPG。
4. **请对端发回来**：必须写本机 `service_id=JiuwenSwarm-Mac`，禁止只写「发给我」。
   - 对 `JiuwenSwarm-*`：让对方用 `a2a_send_message` + `file_paths`
   - 对 `HW-Phone1` / OpenClaw：让对方用 `a2a_send_local_file`（等同 `a2a_send_file`），不要提 `a2a_send_message`
5. 路径已写明：立刻发，不要 glob / bash。
6. 入站已带文件：只确认收到。

向手机要原图的范例（本机 ID 换成 Mac）：

```text
a2a_send_message(
  service_id="HW-Phone1",
  text="请用 a2a_send_local_file（等同 a2a_send_file）把最新照片原文件发给 service_id=JiuwenSwarm-Mac。不要转格式。不要用 a2a_send_message / send_file_to_user。"
)
```

本机把沙箱文件发给另一台 Jiuwen：

```text
a2a_send_message(
  service_id="JiuwenSwarm-Windows",
  text="发送文件：example.pptx，请查收。",
  file_paths=["example.pptx"]
)
```

`text` 必须非空，否则对端会 `empty query` 失败。

### 3.2 每轮注入本机 service_id

`response_prompt_rail.py` 的 `_sync_a2a_transfer_prompt`：从 `JIUWENSWARM_A2A_SERVICE_ID` 读本机 ID，每轮 `before_model_call` 写入 `a2a_transfer` 段。`uninit` 时删掉该段。

没有这个注入，模型不知道自己叫 `JiuwenSwarm-Mac`，请对端发文件时只会写「发给我」。

### 3.3 工具必须是 DIRECT（不要用 always_visible 参数）

`openjiuwen` 的 `DeepAgentConfig` **没有** `progressive_tool_always_visible_tools` 字段。传进去会被丢掉。

`AbilityManager` 只认 ToolCard 上**显式声明**的 `exposure`。正确写法：

```python
from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard, ToolExposure

ToolCard(
    name="a2a_send_message",
    description="...",
    input_params={...},
    exposure=ToolExposure.DIRECT,  # 必须在构造时传入，进入 model_fields_set
)
```

`a2a_registry_list` 同样 `DIRECT`。

对比：openjiuwen `deep_agent.py` 的 `_DEFAULT_DIRECT_TOOL_NAMES` 里已经包含 `send_file_to_user`，不包含 A2A 工具。不设 `DIRECT` 时，模型会先 `tool_search`，再多一轮才真正发送。

`interface_deep.py` 里不要再写：

```python
progressive_tool_always_visible_tools=["a2a_registry_list", "a2a_send_message"]  # 无效
```

### 3.4 `a2a_registry_tools.py` 其它点

- 默认单文件上限：`50 * 1024 * 1024`，环境变量 `JIUWENSWARM_A2A_MAX_INLINE_FILE_BYTES` 可覆盖。
- `_file_part()` 读沙箱文件，超限报错；原样 base64，不转码。
- `a2a_send_message` 先 `a2a.registry.list` 校验 `service_id`，再 `a2a.send`。注册中心结果缓存 8 秒，避免 list+send 打两次。
- 工具卡：问 OpenClaw 用 `a2a_send_local_file`；问 Jiuwen 才写 `a2a_send_message`。范例里的 `JiuwenSwarm-Windows` 在 Mac 上改成 `JiuwenSwarm-Mac`。
- `_default_service_id()`：`darwin` → `JiuwenSwarm-Mac`。

Sidecar 控制面：

- `JIUWENSWARM_A2A_SIDECAR_CONTROL_URL` = `http://127.0.0.1:19802/gateway/call`
- `JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN` 至少 32 字符

### 3.5 入站文件：落到桌面 + 纯投递自动 ACK

`file_sandbox.py`（Windows 树已具备；若 Mac 树较旧，需补齐）：

- `JIUWENSWARM_RECEIVED_FILES_DIR` / `JIUWENSWARM_FILE_SANDBOX_ROOT` 指向 `~/Desktop/JiuwenSwarm`
- `persist_received_file()`、`safe_sandbox_filename()`、`get_received_files_dir()`
- 相对路径相对该目录解析，不要再用 `~/.jiuwenswarm/sandbox`

`a2a_connect.py`：

- `_max_inline_file_bytes()` 默认 50MB，读同一环境变量。
- `map_a2a_parts_to_params` 把 inline 文件 `persist_received_file` 到桌面。
- `inbound_file_delivery_ack_text()`：已有文件且正文是「请查收 / 发送文件 / …」、没有「分析/总结/…」时，**不跑 Agent**，直接回「已收到文件：…，已保存。」发送方不再干等对端一轮模型（大约省 20s）。
- 空 query + 有文件：也自动 ACK。纯空 query 仍失败。

鸿蒙旧包若没有 `persist_received_file`，不能整文件覆盖 `a2a_connect.py`，否则 Gateway `ImportError`。Mac 若 `file_sandbox.py` 已是桌面沙箱版，可以直接对齐 Windows 这份 `a2a_connect.py`。

### 3.6 sidecar 50MB

Jiuwen 默认曾是 2MB；sidecar 插件默认已是 50MB（`52_428_800`）。鸿蒙 sidecar 曾把 `maxInlineFileSizeBytes` 写成 **6MB**，不改的话 Python 放行、sidecar 仍拒。

Mac 的 `~/.jiuwenswarm/a2a-sidecar.json` 建议显式：

```json
"security": {
  "inboundAuth": "none",
  "maxFileSizeBytes": 52428800,
  "maxInlineFileSizeBytes": 52428800
}
```

`openclaw-a2a/a2a-plugin/index.ts` 里 Express JSON body limit 会按 `maxInlineFileSizeBytes * 1.4 + 1MB` 放大，与 50MB 对齐。

---

## 4. Mac 配置（重启前改好）

`~/.jiuwenswarm/config/.env` 增加或改成：

```bash
A2A_SERVER_ENABLED="true"
A2A_SERVER_HOST="127.0.0.1"
A2A_SERVER_PORT="19100"
A2A_SERVER_PATH="/a2a"
A2A_SERVER_PROTOCOL_VERSION="0.3.0"

JIUWENSWARM_A2A_REGISTRY_DATASET="openclaw_devices"
JIUWENSWARM_A2A_SERVICE_ID="JiuwenSwarm-Mac"
JIUWENSWARM_A2A_SIDECAR_CONTROL_URL="http://127.0.0.1:19802/gateway/call"
JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN="<与 a2a-sidecar.json 里 sidecarControl.token 相同，≥32 字符>"

JIUWENSWARM_RECEIVED_FILES_DIR="/Users/<you>/Desktop/JiuwenSwarm"
JIUWENSWARM_FILE_SANDBOX_ROOT="/Users/<you>/Desktop/JiuwenSwarm"
JIUWENSWARM_A2A_MAX_INLINE_FILE_BYTES="52428800"
```

`a2a-sidecar.json` 里 `registry.serviceId`、`tunnel.deviceId`、`agentCard.name` 与上面的 `JIUWENSWARM_A2A_SERVICE_ID` **逐字一致**。

`~/.jiuwenswarm/agent/workspace/memory/MEMORY.md` 建议写入：

```markdown
### 用户文件位置
- A2A 文件在：`/Users/<you>/Desktop/JiuwenSwarm`
- 不要去 `~/.jiuwenswarm/sandbox`

### A2A 传文件
- 本机 service_id 是 `JiuwenSwarm-Mac`。
- 本机发文件只用 `a2a_send_message` + `file_paths`。不要 `send_file_to_user`，不要转 HEIC/JPG。
- 请 JiuwenSwarm 对端发回来：让对方 `a2a_send_message`，service_id 填 `JiuwenSwarm-Mac`。禁止只写「发给我」。
- 请 HW-Phone1 / OpenClaw：对方没有 `a2a_send_message`；让它用 `a2a_send_local_file`（等同 `a2a_send_file`），原文件、不转格式，目标 ID 逐字填 `JiuwenSwarm-Mac`。
- 入站已带文件：只确认收到。当前渠道已是 `a2a` 时，「在吗」直接回复，不要再 `a2a_send_message`。
```

创建桌面目录：`mkdir -p ~/Desktop/JiuwenSwarm`。

---

## 5. 对照表：谁用哪把工具

| 动作 | 谁执行 | 工具 |
|------|--------|------|
| Mac → 另一台 Jiuwen 发文件 | JiuwenSwarm-Mac | `a2a_send_message` + `file_paths` |
| Mac → 让 Windows/HW-PC1 把文件发回来 | 正文指令 | 让对方 `a2a_send_message`，`service_id=JiuwenSwarm-Mac` |
| Mac → 让手机发原图 | 正文指令 | 让对方 `a2a_send_local_file`，`service_id=JiuwenSwarm-Mac` |
| 网页里给人下载 | 当前 web 会话 | `send_file_to_user`（默认 direct，容易误用，提示词必须挡掉） |

sidecar 方法始终是 `a2a.send`。名称差异只在 **LLM 工具层**。

---

## 6. 建议移植顺序

1. 对齐 `file_sandbox.py`（桌面目录 + `persist_received_file`），再覆盖 `a2a_connect.py`。
2. `a2a_registry_tools.py`：`DIRECT` + 50MB + OpenClaw 文案；范例 ID 改 `JiuwenSwarm-Mac`。
3. `prompt_builder.py` + `response_prompt_rail.py` + `send_file_to_user.py` 描述。
4. `.env` / sidecar JSON / `MEMORY.md`。
5. 跑上面的单测，重启栈。
6. 冒烟：Mac 问 Windows「在吗」（应几十秒内、无嵌套 send）；再要一个小文件；再让 HW-Phone1 发一张原图（正文应出现 `a2a_send_local_file`）。

---

## 7. 源码锚点（方便在 Mac 树上搜）

| 符号 | 文件 |
|------|------|
| `_a2a_transfer_prompt` | `agents/harness/common/prompt/prompt_builder.py` |
| `_sync_a2a_transfer_prompt` | `agents/harness/common/rails/response_prompt_rail.py` |
| `A2ARegistryToolkit.get_tools` | `agents/harness/common/tools/a2a_registry_tools.py` |
| `_file_part` / `a2a_send_message` | 同上 |
| `_default_service_id` | 同上（darwin → `JiuwenSwarm-Mac`） |
| `inbound_file_delivery_ack_text` | `gateway/channel_manager/protocol/a2a/a2a_connect.py` |
| `_max_inline_file_bytes` | 同上 |
| `persist_received_file` | `common/file_sandbox.py` |
| `_DEFAULT_DIRECT_TOOL_NAMES` | `openjiuwen/harness/deep_agent.py`（site-packages） |
| `_apply_tool_exposure_policy` | `openjiuwen/core/single_agent/ability_manager.py` |
| `maxInlineFileSizeBytes` 默认 52428800 | `openclaw-a2a/a2a-plugin/index.ts` |

入站信封目前仍不带来源 `service_id`。要彻底不靠提示词猜「发给我是谁」，需要改信封或让工具默认回给来源设备；本次没有做这块，只靠**发送方把本机 ID 写进 text**。
