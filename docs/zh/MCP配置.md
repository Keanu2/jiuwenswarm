# MCP 服务配置

JiuwenSwarm 支持通过 MCP（Model Context Protocol）接入外部工具服务。MCP 使 Agent 可以调用由第三方提供的工具，扩展能力范围。

`command.mcp` 临时连接与取工具共用时限，未配置时默认 10 秒；服务显式配置的有限正数 `timeout_s` 优先，允许慢启动服务使用更长预算。临时连接只执行一次断开清理，另限 5 秒，不与调用补丁重复计时。超时返回 `ok=false / MCP_TIMEOUT`，连接失败返回 `MCP_CONNECTION_FAILED`，正常空服务仍返回 `ok=true` 和空工具列表。普通 HTTP 工具调用仍使用原调用预算，超时后的清理限 5 秒。

上述失败响应适用于工具发现请求（`list_tools`）。`show <name>` 以展示配置为主，补充工具数量失败时仍返回配置详情，并以 `tool_count: null` 和 `tool_count_error` 标明数量未知及失败原因；正常空服务的数量为 `0`。不带名称的 `show` 只列启用项，不连接服务。

预热池中的排队、Agent 创建与 `prepare_session` 保留 120 秒总预算，兼容正常冷启动，失败清理另限 5 秒；超时向等待方传播，调用方不会继续在已清理实例上执行。超时的后台槽位自动重新排队，不依赖下一次配置 `sync`，且不复用失败会话 ID。此总预算不保证消除 15/30 秒的 stall 诊断；慢服务或负载下的 stall 仍需部署回归确认。

---

## 两种 MCP 使用场景

| 场景 | 配置方式 | 说明 |
|------|---------|------|
| **浏览器运行时 MCP** | 环境变量（`.env`） | Playwright 浏览器自动化的 MCP 封装，详见 [浏览器工具](浏览器.md) |
| **通用 MCP Server** | `config.yaml` → `mcp.servers` | 接入任意 MCP 服务（stdio / sse / streamable-http），本文档重点 |

---

## 配置 MCP Server

在 `config/config.yaml` 中添加 MCP 服务：

```yaml
mcp:
  servers:
    - name: my-local-tool
      enabled: true
      transport: stdio
      command: python
      args: ["path/to/server.py", "--transport", "stdio"]
      cwd: .
      env:
        LOG_LEVEL: INFO

    - name: remote-api
      enabled: false
      transport: sse
      url: http://127.0.0.1:9000/sse
      headers:
        Authorization: Bearer your-token-here
      timeout_s: 30

    - name: remote-streamable
      enabled: true
      transport: streamable-http
      url: http://127.0.0.1:8000/mcp
      timeout_s: 60
```

---

## 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 服务标识，需全局唯一 |
| `enabled` | bool | 是 | 是否启用该服务 |
| `transport` | string | 是 | 传输协议：`stdio`、`sse`、`streamable-http` |
| `command` | string | stdio 时必填 | 启动命令（如 `python`、`npx`） |
| `args` | list | 否 | 命令参数列表 |
| `cwd` | string | 否 | 工作目录 |
| `env` | map | 否 | 传给子进程的环境变量 |
| `url` | string | sse/streamable-http 时必填 | 服务端地址 |
| `headers` | map | 否 | 请求头（如 Authorization） |
| `timeout_s` | int | 否 | 请求超时秒数 |

---

## 传输协议对比

| 协议 | 适用场景 | 说明 |
|------|---------|------|
| **stdio** | 本地工具服务 | Agent 启动子进程，通过标准输入/输出通信。适合本地 Python/Node 脚本 |
| **sse** | 远程 HTTP 服务 | Server-Sent Events 协议。适合已有 HTTP 服务端 |
| **streamable-http** | 远程 HTTP 服务（推荐） | 基于 HTTP 的流式协议，性能优于 SSE。推荐远程服务使用 |

---

## Agent 侧管理命令

在对话中使用 `/mcp` 命令管理 MCP 服务：

```
/mcp list              # 列出已配置的 MCP 服务及状态
/mcp reload            # 重新加载所有 MCP 服务
/mcp enable <name>     # 启用指定服务
/mcp disable <name>    # 禁用指定服务
```

---

## 浏览器 MCP 运行时

浏览器自动化使用独立的 MCP 运行时配置，通过环境变量控制：

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `BROWSER_RUNTIME_MCP_ENABLED` | 是否启用浏览器 MCP 运行时 | `1` |
| `BROWSER_RUNTIME_MCP_CLIENT_TYPE` | 客户端类型：`streamable-http` / `sse` / `stdio` | `streamable-http` |
| `BROWSER_RUNTIME_MCP_SERVER_PATH` | MCP 服务地址 | `http://127.0.0.1:8940/mcp` |
| `BROWSER_RUNTIME_MCP_TIMEOUT_S` | 超时秒数 | `300` |
| `BROWSER_RUNTIME_MCP_HOST` | 本地包装器主机 | `127.0.0.1` |
| `BROWSER_RUNTIME_MCP_PORT` | 本地包装器端口 | `8940` |

详见 [浏览器工具](浏览器.md)。

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `BROWSER_RUNTIME_MCP_ENABLED` | 启用/禁用浏览器 MCP 运行时 |
| `BROWSER_RUNTIME_MCP_CLIENT_TYPE` | 浏览器 MCP 客户端类型 |
| `BROWSER_RUNTIME_MCP_SERVER_PATH` | 浏览器 MCP 服务地址 |
| `BROWSER_RUNTIME_MCP_TIMEOUT_S` | 浏览器 MCP 超时 |

---

## 详见

- [浏览器工具](浏览器.md) — 浏览器 MCP 运行时详细配置
- [配置说明](配置信息.md) — 完整配置参考
