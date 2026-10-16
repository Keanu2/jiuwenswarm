# WebSocket 身份认证与会话归属

`/ws`、`/ws/git` 不再信任 query `user_id`、`X-User-Id` 或参数中的用户身份。
服务端根据访问凭据绑定用户。模型 API Key 与网关访问 Token 是不同凭据。

## 桌面和本地兼容

小艺 Work 桌面继续使用命名管道、对端进程镜像校验及首帧 `e2aToken`。
管道认证后保留整个单用户工作区的访问能力，不新增 TCP listener，不要求额外输入
Web Token；原有会话、项目、工具、cron、Git 和流式推送保持原工作区语义。

未配置 Web Token 时，只允许**实际连接两端均为回环地址**的本地连接，身份固定为
`local`；浏览器 Origin 也必须为本地 HTTP(S) 来源。客户端字段不能切换用户。
这保留本机 CLI、Web UI 的用法，不作为多用户隔离模式。非回环 WebSocket 或生产
Web HTTP listener 缺少认证配置时拒绝启动。

内置 HTTP 反向代理检查实际外部连接，无认证的远程连接不能借回环转发获得本地例外。
外部 Nginx 等代理必须配合认证部署，不能仅因后端绑定回环就省略认证。

## 单用户服务器

`JIUWENSWARM_WEB_TOKEN` 为至少 32 字节、无空白的随机 Token。
`JIUWENSWARM_WEB_USER_ID` 可设置固定身份，默认 `local`。该凭据具有工作区管理权限，
保留单用户 Web UI 的全部功能和旧数据访问范围。
如接入依赖用户命名空间的 AgentOS 后端，固定身份应配置为原先实际使用的用户 ID。

```bash
export JIUWENSWARM_WEB_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
# 在相同环境中启动 Gateway 和生产 Web HTTP 服务。
```

PowerShell 中可用 Python 生成随机值并赋给 `$env:JIUWENSWARM_WEB_TOKEN`。
凭据不放入 URL、启动命令参数或日志。网络部署使用 HTTPS/WSS 或受信隧道。

## 多用户服务器

使用 `JIUWENSWARM_WEB_USERS` 配置 `用户 ID -> 独立随机 Token` 的 JSON 对象，
与 `JIUWENSWARM_WEB_TOKEN` 互斥。Token 至少 32 字节、不能重复、不能含空白。
用户 ID 只允许字母、数字、下划线、点和连字符，不能以点开头，最多 200 字符。

```text
JIUWENSWARM_WEB_USERS={"alice":"<alice-random-token-at-least-32-bytes>","bob":"<bob-random-token-at-least-32-bytes>"}
```

尖括号内容须替换为独立随机值，部署凭据不要提交进仓库。普通用户可创建会话和项目，
列出自己的资源，访问自己会话的聊天、历史、目标状态和自己项目的 Git 功能。
共享模型密钥、频道、全局 cron、技能安装等工作区级控制要求管理员权限，不能通过
附加一个自己拥有的 `session_id` 绕过限制。普通用户新建项目使用服务端分配的
`web-workspaces/<user-hash>/` 子目录；显式路径也必须位于该范围，防止把他人的历史
目录包装为自己的 Git 项目。桌面与管理员保留选择任意已授权宿主目录的原行为。

需要 Web 管理员时，显式设置 `JIUWENSWARM_WEB_ADMINS=alice`，多个 ID 用逗号分隔。
管理员必须已存在于 `WEB_USERS`，可访问整个工作区，包括旧数据和其他用户资源。
客户端不能通过 Header、query 或角色字段提升权限。相邻 `/tui`、`/acp` 入口同样要求
工作区管理员，普通 Web 用户不能借它们绕过归属校验。

这是共享运行时的**控制面资源权限**，未增加操作系统级租户沙箱。Agent 工具和宿主
文件系统仍使用部署授予的权限；互不信任的用户运行代码时，应分别部署工作区、进程
和系统访问边界。工作区管理员属于受信任主体。
AgentServer 后端保持回环或受控网络访问，仅供网关调用，不作为普通用户直连入口。

## 客户端与重连

- 原生客户端使用握手头 `Authorization: Bearer <web-token>`。
- Python CLI 和非桌面 ACP／频道控制客户端支持 `JIUWENSWARM_WEB_CLIENT_TOKEN`。
  同部署的默认本地端口可沿用单用户 `WEB_TOKEN`；远程或自定义目标需显式配置客户端
  Token，不从多用户映射自动选择他人的凭据。TUI／ACP 和全局频道控制需要管理员凭据。
- 浏览器收到 `auth.required` 后先发送 `{"type":"auth","token":"<web-token>"}`。
  服务端验证后返回 `auth.ok`，随后注册路由、运行连接钩子、处理 RPC。
- 认证失败关闭码为 `4401`，认证首帧等待上限 10 秒；认证前不发送业务 ACK 和数据。
- 内置 Web UI 显示访问令牌输入框，Token 保存在当前页签 sessionStorage；主 RPC、Git
  socket 都在业务帧前发送认证帧，错误 Token 不无限自动重连。
- 生产 HTTP 服务 `/web-auth/session` 用 Bearer 交换建立 HttpOnly、SameSite=Strict 的
  Cookie，兼容现有浏览器文件请求；HTTP 请求仍检查 Origin。原生 HTTP 客户端可直接
  使用 Bearer。此接口不返回 Token。
  Cookie 在同源浏览器上下文共享；不同用户并行使用时请采用独立浏览器配置或隐私窗口。
- 修改或撤销凭据后，重启 Gateway 和 Web HTTP 服务以关闭旧连接。

RPC 格式、流式事件和会话 ID 格式不变。本地回环和桌面维持原接入方式；原先没有认证
的远程客户端需要增加凭据，这是必要的安全迁移。

## 归属与投递

安全归属保存在工作区 `.web-owners/{sessions,projects}/`，使用资源 ID 的 SHA-256
作为目录名，独占创建、同步落盘，重启后有效。该目录应随工作区备份并限制修改权限。
会话在服务端确认创建响应后记录归属，创建幂等键按用户隔离。客户端字段、旧 metadata
里的 `user_id` 和首次访问均不能成为认领依据。

列表在排序、分页、统计前过滤。历史、修改、删除、恢复、Git 订阅及嵌套目标会话引用
检查归属；授权发生在路由注册、附件下载和 Agent 转发之前。出站在进入发送队列前
再次检查，覆盖 `ws_id` 寻址、session 回退、流式事件与广播。没有明确资源归属的全局
cron、主动推荐和配置推送仅给管理员。Git watcher 保留连接级 `watch_id` 校验。

HTTP `/share-api/snapshot` 同样检查会话归属。工作区文件浏览、原始路径读写和通用
`/api` 代理属于管理员操作；已有签名文件下载链接保留原能力令牌校验。

## 旧数据迁移

桌面及单用户管理员继续访问所有旧数据。多用户普通身份看不到没有明确安全归属的
旧资源，也不能通过首次访问认领。管理员核实归属、停服并备份后，可在正确工作区
配置下逐条登记：

```python
from jiuwenswarm.common.security.web_identity import WebPrincipal, claim_resource

claim_resource("sessions", "<verified-existing-session-id>", WebPrincipal("alice"))
claim_resource("projects", "<verified-existing-project-id>", WebPrincipal("alice"))
```

不要按旧 metadata 中未经认证的用户字段自动批量分配。已有记录不能被其他用户覆盖；
更正错误归属需要管理员停服、备份后审查对应记录。

## 开发与测试

Vite 包含调试和本地文件接口，仅允许回环连接及本地浏览器 Origin，包含 HMR／WS 升级。
远程使用构建后的生产 Web 服务与认证配置。

后端回归：`tests/unit_tests/channel/test_web_identity_security.py`、`test_web_identity_http.py`。
前端回归：`npm run test:web-auth`、`npm run test:dev-workspace-security`。
