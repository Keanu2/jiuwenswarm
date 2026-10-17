# 推理代理安全配置与兼容迁移

管理接口 `/api/v1/proxies` 与推理代理数据端口（常用 8322）是两个独立入口。
`JIUWENBOX_API_TOKEN` 保护管理接口；`JIUWENBOX_PROXY_TOKEN` 保护数据端口。
代理默认禁用，启用后建议绑定 `127.0.0.1`。绑定非回环地址必须设置独立代理 Token，
否则启动失败且不开启代理监听。配置 Token 后，本地请求也必须认证。

## 固定上游权限

启动 YAML 中 `routes` 的目标 Origin 自动获得批准，保持已配置的公网、本地 IP、
企业内网 IP 模型兼容。Origin 由协议、规范化主机名和有效端口共同确定，路径不参与比较。
管理 API 新建或修改路由只能选用已批准的 Origin，不能扩展权限；违规返回 `403`。
删除全部路由仍保留部署权限，不能通过删除再创建绕过限制。

`allowed_origins` 是部署管理员配置的额外精确白名单，不支持通配符、用户名密码、
查询参数或路径。需要在运行时切换的公网服务、额外本地模型端口，应事先列入白名单。
**企业域名如果解析为内网地址，也必须明确列入 `allowed_origins`**；启动路由中的
本地和内网 IP，以及 `localhost`，无需重复列入。

每次连接检查全部 DNS A/AAAA 结果，只要存在被禁止地址就拒绝；随后直接连接已检查的
数值地址，TLS SNI、证书主机名和 HTTP Host 保持原始目标语义，不进行第二次域名解析。
未明确允许内网访问的公网域名不能通过 DNS 变化指向内网。即使列入白名单，云元数据
常见地址、链路本地、未指定、多播、保留地址及受限制的 IPv6 转换地址仍被禁止。
代理不跟随上游重定向，不受环境 HTTP 代理变量影响。

```yaml
inference_privacy_proxies:
  listen_host: "127.0.0.1"
  listen_port: 8322
  allowed_origins:
    - "https://models.corp.example:8443"  # 企业域名可解析为内网地址
    - "http://127.0.0.1:11434"            # 允许 API 后续新增本地模型路由
  # 可选：附加企业 CA 的 PEM 文件，保留系统公共 CA 和主机名校验
  # ca_file: "/etc/jiuwenbox/corp-ca.pem"
  routes:
    - path_prefix: "openai"
      target_endpoint: "https://api.openai.com"
      api_key: "<upstream-api-key>"
    - path_prefix: "corp"
      target_endpoint: "https://models.corp.example:8443/v1"
      api_key: "<corporate-model-api-key>"
```

修改部署白名单或 CA 后重启服务。`routes: []` 配合 `allowed_origins` 可预先授权目标，
随后通过管理 API 创建、启动路由。API 请求无法修改白名单、CA 文件或监听地址。

## TLS 与自签名证书

默认验证证书链和主机名。企业 CA / 自签名部署优先使用 `ca_file`，也可安装到系统信任库。
单独在管理 API 提交 `skip_cert_verify: true` 会被拒绝。确有调试需要时，管理员可在 YAML
的 `insecure_tls_origins` 中精确列出允许跳过验证的 HTTPS Origin，再为该路由设置
`skip_cert_verify: true`；默认列表为空。该例外不扩大目标白名单，也不放行元数据地址。

## 客户端认证

仅回环监听、实际连接两端均为回环地址且未设置代理 Token 时，原有占位 API Key 客户端
继续工作。需要远程访问时，设置 `listen_host: "0.0.0.0"`（或指定接口 IP），并通过环境
变量提供随机 Token：

```bash
export JIUWENBOX_PROXY_TOKEN="$(openssl rand -hex 32)"
# 使用现有服务启动命令，并加载上述 policy YAML
```

配置了上游 `api_key` 的常见 SDK 无需增加自定义请求头：OpenAI SDK 把原来假的 `api_key`
改为代理 Token；Anthropic SDK 同样把 `api_key` 改为代理 Token。服务端先校验
`Authorization: Bearer <proxy-token>` 或 `X-Api-Key: <proxy-token>`，再替换为真正上游 Key。
模型 base URL 与请求格式保持不变。

需要透传客户端自己的上游 Key，使用独立请求头 `Proxy-Authorization: Bearer <proxy-token>`，
并继续通过 `Authorization` / `X-Api-Key` 提供上游凭据。该代理认证头始终移除；通过普通
认证头提供的代理 Token 也不会透传到上游。没有上游 Key 的本地模型同样支持代理认证。
重复认证头或错误 Token 被拒绝。每个 keep-alive 请求分别认证、匹配路由及注入 Key。

数据 listener 本身提供 HTTP；跨不可信网络访问时，应通过 HTTPS 入口或受信隧道承载
请求，防止代理 Token 和模型内容在传输途中暴露。

## Docker 与功能验证

`run_docker.sh` 默认将代理端口发布到宿主 `127.0.0.1`，通过
`JIUWENBOX_PROXY_HOST_BIND` 显式变更宿主绑定地址。脚本按变量名向容器传递
`JIUWENBOX_PROXY_TOKEN`，不把 Token 写入 Docker 命令行。容器内若绑定 `0.0.0.0`，
即使宿主只发布到回环地址，也必须配置代理 Token。管理 API Token 仍单独配置。
通过 Docker 发布端口访问代理时，容器内应设置 `listen_host: "0.0.0.0"` 并配置 Token，
宿主发布地址仍可保持回环；容器内绑定 `127.0.0.1` 无法通过常规端口映射访问。

支持 HTTP/1.1、Content-Length / chunked 上传、二进制正文、SSE、分段请求头、
`Expect: 100-continue`、客户端 keep-alive、API Key 注入、原始编码路径和查询参数。
每个请求建立经过校验的上游连接；上游不复用，客户端连接可继续复用。
CONNECT、协议升级和有歧义的请求分帧被拒绝。

热更新会关闭旧代理连接，因此进行中的流式请求需要客户端重试；非法目标更新在关闭
旧 listener 前被拒绝，不影响原有路由。成功更新会恢复原先启用的全部路由。

本地回归：`tests/unit/test_inference_proxy_security.py` 覆盖目标权限、DNS 混合结果、
TLS CA / SNI / 主机名、逐请求认证、流式与二进制传输和路由热更新。
真实公网模型、企业网络和 Docker 部署应按上述配置补充环境验收。
