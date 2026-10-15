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
