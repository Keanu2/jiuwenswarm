# Channels

## Concept Overview

### What is a Channel?

A **Channel** is JiuwenSwarm's message ingress layer. As a unified messaging hub, it connects various external chat platforms (such as Feishu, DingTalk, WeCom, Telegram, etc.) and provides the following core capabilities:

| Capability | Description |
|------------|-------------|
| **Message Ingestion** | Receives messages from different platforms, normalizes them, and forwards them to JiuwenSwarm for processing |
| **Multi-platform Sync** | A single JiuwenSwarm service can connect to multiple platforms simultaneously, with messages interoperable across them |
| **Identity Mapping** | Establishes unique identifiers for users on each platform and maintains independent conversation contexts |
| **Security Isolation** | Message flows across channels are isolated from each other; supports whitelists, permission controls, etc. |

### Relationship between Channels, Web Chat, and Agents

These three components form JiuwenSwarm's complete conversation system:

- **Channel**: The message ingress layer that receives and normalizes messages from different platforms. Besides external channels like Feishu and Xiaoyi, **the Web UI itself is also a built-in channel**.
- **Agent**: The message processing engine. All messages from any channel are ultimately processed by an Agent for understanding, reasoning, and response.
- **Web Chat**: A built-in web channel that provides a visual interface for users to interact directly with Agents.

**Data Flow**: User message → Any channel (Feishu/Xiaoyi/Web, etc.) → JiuwenSwarm Gateway → Agent processing → Response → Original channel → User

### Introduction

JiuwenSwarm's **Channels** are the **gateways** through which you converse with different chat platforms. JiuwenSwarm has already achieved seamless integration with **HarmonyOS Xiaoyi**, **Feishu**, and more, with continuous expansion to additional platforms. You can talk to JiuwenSwarm directly through **Feishu**, the **Xiaoyi app on HarmonyOS devices**, and others.

### Digital Avatar

JiuwenSwarm supports **Group Digital Avatar** on **Feishu** and **WeCom** channels. When enabled, the bot acts as a designated user's "digital avatar" in group chats — it automatically identifies messages relevant to that user and replies on their behalf in first person. For personal action items such as to-dos and reminders, the avatar sends the reply as a private message to the user while posting a brief confirmation in the group. Irrelevant messages are filtered out automatically, saving Agent resources.

This feature is disabled by default. See the configuration instructions under each channel below.

### Configuration Methods

This tutorial focuses on **Web UI configuration**. Subsequent channel setup instructions are all based on web interface operations.

For manual configuration, edit the `config.yaml` file (default path: `~/.jiuwenswarm/config/config.yaml`), set the corresponding channel to `enabled: true` and fill in the credentials. Changes take effect automatically upon saving.

- **Edit `config.yaml` manually** — Default location is `~/.jiuwenswarm/config/config.yaml` (created automatically on first `jiuwenswarm-start`). Set the desired channel to `enabled: true` and fill in the credentials; saving triggers an automatic reload without restarting.

### Channel Feature Differences

Different channels have varying capabilities regarding private/group chat support, trigger methods, and permission requirements:

| Feature | Description |
|---------|-------------|
| **Private/Group Chat** | Some channels only support private chat (e.g., Xiaoyi), while others support both private and group chats (e.g., Feishu, WeCom) |
| **Trigger Methods** | Private chats typically trigger directly via conversation; group chats may require @mentioning the bot or using specific command prefixes |
| **Permission Requirements** | Different platforms have varying permission requirements for bots, such as message read permissions, group management permissions, etc. |

See the detailed setup instructions for each channel for specific differences.

---

## Channel Setup

JiuwenSwarm supports integration with multiple chat platforms, divided into the following categories based on region:

### China Channels

| Channel | Description |
|---------|-------------|
| [Xiaoyi](ChinaChannels.md#xiaoyi) | Huawei HarmonyOS intelligent assistant, private chat only |
| [Feishu](ChinaChannels.md#feishu-lark) | Enterprise collaboration platform, supports private and group chat, with Digital Avatar feature |
| [DingTalk](ChinaChannels.md#dingtalk) | Enterprise collaboration platform, supports private and group chat |
| [WeCom](ChinaChannels.md#wecom-wechat-work) | Enterprise messaging tool, supports private and group chat, with Digital Avatar feature |
| [Personal WeChat](ChinaChannels.md#personal-wechat) | Personal messaging tool, private chat only |

For detailed configuration instructions, see: [China Channels](ChinaChannels.md)

### International Channels

| Channel | Description |
|---------|-------------|
| [Telegram](InternationalChannels.md#telegram) | International messaging tool, supports private and group chat |
| [Discord](InternationalChannels.md#discord) | Gaming community platform, supports private and group chat |
| [Slack](InternationalChannels.md#slack) | Enterprise collaboration platform, supports DMs and channel threads |
| [WhatsApp](InternationalChannels.md#whatsapp) | Personal messaging tool, supports private chat |

For detailed configuration instructions, see: [International Channels](InternationalChannels.md)

---

## Developer Channels

In addition to the end-user chat platforms above, JiuwenSwarm also provides integration methods for developers:

| Integration | Description |
|-------------|-------------|
| [ACP Plugin Usage](ACP_Client_Config.md) | Integrate with JiuwenSwarm via the ACP protocol, suitable for custom integrations |
| [SSH](#ssh) | Connect with an authorized public key and reach AgentOS / sandboxes through `ssh.relay` |

### Web URL attachments

WebChannel retains legacy URL attachments on `chat.send`, for example:

```json
{
  "type": "req",
  "id": "chat-with-attachment",
  "method": "chat.send",
  "params": {
    "content": "Please review the attachment",
    "files": [{"name": "report.xlsx", "url": "https://files.example/report.xlsx?signature=..."}]
  }
}
```

`uri` / `filename` remain aliases for `url` / `name`. URL downloads are processed only for `chat.send`;
other methods with URL attachment lists are rejected before network or filesystem I/O.
Current Web image and document uploads still use `media.persist` / `document.persist`. Persisted
`files.uploaded_images`, `files.uploaded_documents`, and existing local `path` references bypass this
downloader and retain their existing behavior.

#### Public and private sources

Public HTTP and HTTPS downloads need no allowlist. Custom ports, signed paths and queries, and up to five
redirects are supported. URLs are limited to 16384 characters and cannot contain embedded credentials,
raw whitespace, or control characters. Every redirect destination is revalidated. The connector uses checked numeric DNS
answers directly instead of performing an unchecked second lookup. Any disallowed answer rejects the entire
download, covering IPv4, IPv6, and IPv4-mapped addresses. Noncanonical decimal, short, and octal numeric IP
representations cannot bypass the resolver checks.

Private and loopback addresses are denied by default. For an internal attachment service, an administrator
must set `WEB_ATTACHMENT_PRIVATE_ORIGINS` in the **Gateway process environment**, then restart Gateway:

```bash
export WEB_ATTACHMENT_PRIVATE_ORIGINS="https://files.corp.example:8443,http://10.20.30.40:8080"
# Start Gateway with the existing deployment command.
```

PowerShell:

```powershell
$env:WEB_ATTACHMENT_PRIVATE_ORIGINS = "https://files.corp.example:8443,http://10.20.30.40:8080"
```

Setting this only in a frontend or Agent sandbox has no effect. The allowlist adds private-network exceptions;
it does not restrict existing public downloads. Entries match the exact scheme, hostname, and effective port.
IDNA hostnames and IPv6, such as `http://[fd12::10]:8080`, are supported. Wildcards, hostname suffix rules,
credentials, paths, queries, fragments, and trailing `/` are rejected. Omitted ports mean HTTP 80 or HTTPS 443.
Hostnames are normalized using DNS semantics; subdomains and different ports do not match.
Allowlisting trusts the service and its DNS, so configure only controlled file services.

Known metadata endpoints, link-local, multicast, unspecified, reserved, and special ranges such as IPv6
transition networks remain forbidden even when allowlisted. This also applies to redirect and subsequent DNS
destinations. Downloads do not inherit environment proxies or send browser cookies, session credentials, or
client Authorization headers.

#### Storage and resource limits

Files are saved under the current Agent workspace at `attachments/<server-generated-ID>/<filename>`.
Normal filenames, Unicode, spaces, and extensions are retained. Duplicate filenames cannot overwrite each
other or workspace files. Path separators, drive paths, UNC paths, Windows ADS/device names, and control
characters are rejected. Symlinks and Windows junctions below the workspace are not followed. Windows holds
parent directory handles against replacement; Linux uses directory FDs, `O_NOFOLLOW`, and exclusive creation.
New Windows directories inherit the workspace ACL so existing sandbox-account access is retained, rather than
being replaced with a private DACL.

After downloading, the gateway supplies `path`, `filename`, and `size_bytes`, removing the original `url` /
`uri` and path aliases that could override the saved file. Downstream consumers do not refetch the raw URL.
Empty files are valid. If any attachment is rejected or fails download/storage, the request returns an error
before entering the Agent, without forwarding an unchecked URL. Files already saved by that request are
cleaned up. Cancellation also cleans files belonging to an incomplete request.

| Gateway environment variable | Default | Meaning |
| --- | --- | --- |
| `WEB_ATTACHMENT_PRIVATE_ORIGINS` | Empty | Comma-separated exact origins allowed to use private addresses |
| `WEB_ATTACHMENT_MAX_BYTES` | `67108864` (64 MiB) | Maximum bytes per downloaded file; checks declared and actual decompressed sizes |
| `WEB_ATTACHMENT_TIMEOUT_SECONDS` | `60` | Total time per download, including DNS, connection, reads, and all redirects |
| `WEB_ATTACHMENT_MAX_FILES` | `16` | URL attachments per `chat.send`; persisted local references do not count |

At most four downloads run concurrently. Connection and socket-read phases also have ceilings of 15 and 30
seconds respectively. All three resource settings must be positive integers; invalid configuration prevents
WebChannel initialization. Configure larger limits explicitly for large attachments or slow file services and
restart Gateway.

Failures use the existing WebSocket `res` format with `ok: false`, a readable `error`, and a `code`:
`ATTACHMENT_REJECTED`, `ATTACHMENT_INVALID_NAME`, `ATTACHMENT_TOO_MANY_FILES`, `ATTACHMENT_TOO_LARGE`,
`ATTACHMENT_DOWNLOAD_FAILED`, or `ATTACHMENT_SAVE_FAILED`. Rejection logs contain only the code, without
download credentials or signed URL parameters.

On upgrade, clients relying on private downloads must allowlist the specific file service. Use the gateway's
supplied local path rather than assuming workspace-root storage or overwriting duplicate names. These rules
apply to the legacy WebChannel downloader; Agent tools retain their own network and filesystem policies.

Run regressions from the repository root:

```bash
python -m pytest tests/unit_tests/channel/test_web_attachment_security.py tests/unit_tests/channel/test_web_attachment_storage.py -o addopts= --asyncio-mode=auto -q
```

Tests use simulated responses and a local temporary HTTP server, never public or metadata services. Windows
tests cover junctions, directory replacement, and long paths. The POSIX directory-FD race test must run on Linux.
Source regressions do not replace deployed checks for public files, CDN redirects, private allowlisting, and uploads.

### SSH

The SSH entry point is controlled by `channels.ssh`. It requires `gateway.agent_client.type: agentos_router` and the optional dependency:

```bash
pip install "jiuwenswarm[ssh]"
```

The bundled configuration defaults to `enabled: false`, so no SSH listener is created. The default address is `127.0.0.1:2222`. Servers can explicitly bind to an interface address or `0.0.0.0`; public key authentication applies to every listening address.

#### Configure authorized keys

Generate a key pair on the client:

```bash
ssh-keygen -t ed25519 -f ./alice_agent_key -C alice
```

Have an administrator copy the contents of `alice_agent_key.pub` into `/etc/jiuwenswarm/ssh/alice_authorized_keys` on the **gateway host**. Keep the private key on the client. The file uses OpenSSH `authorized_keys` format, can contain multiple public keys on separate lines, and must be readable by the gateway process.

Merge this example into the active instance's `config.yaml`, preserving existing AgentOS backend connection settings:

```yaml
gateway:
  agent_client:
    type: agentos_router
channels:
  ssh:
    enabled: true
    listen_host: "127.0.0.1"
    listen_port: 2222
    host_key_path: ~/.jiuwenswarm/config/ssh_host_key
    authorized_keys:
      alice: /etc/jiuwenswarm/ssh/alice_authorized_keys
    relay_timeout_sec: 3600
```

Each `authorized_keys` entry maps an allowed SSH login username to its authorization file on the gateway host. Paths support `~` expansion. Configure users separately: the login username becomes the AgentOS `user_id` in `ssh.relay`. A key authorized for one username cannot authenticate as another username unless explicitly authorized there too.

For remote access, change `listen_host` as needed. When binding to `0.0.0.0`, set `advertise_host` to a real IP address or hostname reachable by clients so endpoint responses contain a usable address. Clients connect to that real address. See the [YuanRong deployment guide](../../deploy/yuanrong/README.md#ssh-入口认证) for instance paths and deployment templates.

#### Authentication and upgrades

- Password and keyboard-interactive authentication are disabled. Arbitrary passwords, missing credentials, unknown users, and unauthorized keys are rejected.
- AsyncSSH checks the authorized key for the username, verifies the private-key signature, and enforces restrictions such as `from`. Failed checks cannot fall back to unconditional acceptance.
- If SSH is enabled but `authorized_keys` is missing, or a file is missing, unreadable, or contains no valid entries, the SSH channel fails to start without opening its listener. Check the authorization configuration and files indicated by the startup error.
- Upgrading from an older version requires configuring authorized keys before using SSH. Arbitrary passwords and keys are no longer accepted. Disabled channels are unaffected.
- Authorization files are read at channel startup. Restart the SSH channel or Gateway after changing or revoking keys; also disconnect existing sessions when revoking access.

`host_key_path` identifies the gateway server to clients. `gateway.agentos.ssh.client_keys_dir` contains private keys for the gateway's southbound connection to YuanRong. Neither replaces inbound user authentication in `channels.ssh.authorized_keys`.

#### Connect and verify

```bash
ssh -o IdentitiesOnly=yes -i ./alice_agent_key -p 2222 alice@<gateway-host>
```

After authentication, `ssh.relay` forwards the session to AgentOS / its sandbox. For an interactive connection, first use `3rdagent.switch` to select an Agent with a sandbox. If no Agent has been selected or the current Agent is the built-in Agent, the first token of an SSH remote command can select `agent_type`. Backend configuration determines whether the relay is available.

During deployment acceptance, verify that an authorized user can connect. An unauthorized private key, a wrong username, and the following password-only attempt must fail authentication without triggering `ssh.relay`:

```bash
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -p 2222 alice@<gateway-host>
```

With `enabled: false`, verify that the SSH service no longer listens on its configured port. Local regression coverage in `tests/unit_tests/channel/test_ssh_auth.py` exercises real SSH handshakes and verifies that unauthorized clients never reach the relay. Validate the deployed environment separately using the steps above.
