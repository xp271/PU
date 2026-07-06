# agent-core QQ 机器人接入计划书

## 约束条件

- 基于现有 wechat-kf 实现的 channel + bridge 架构，最大化代码复用
- agent-core 核心代码零修改
- 使用 QQ 官方 Python SDK（`botpy`），不自行实现底层协议

---

## 一、微信客服 → QQ 机器人：差异分析

### 1.1 现有微信客服架构回顾

```
channels/wechat_kf/
    types.py      — WechatKFConfig, 消息类型
    crypto.py     — AES-256-CBC 加解密 + SHA1 验签
    token.py      — AsyncTokenManager (access_token 自动刷新)
    dedup.py      — DedupCache (msg_id TTL+LRU 去重)
    sync.py       — MessageSyncer (sync_msg 拉取 + cursor 持久化)
    sender.py     — MessageSender (send_msg 推送)
    server.py     — Starlette HTTP 路由
    channel.py    — WechatKFChannel 主调度器

bridge/
    agent_bridge.py    — AgentBridge (SessionState 管理 + Agent 生命周期)
    text_splitter.py   — 长文本拆分 (2000字符限制)
```

### 1.2 差异对比

| 维度 | 微信客服 | QQ 机器人 | 影响 |
|------|---------|----------|------|
| **通信协议** | HTTP 回调 + 主动轮询 | **WebSocket**（长连接推送） | `server.py` 大变，`sync.py` 不需要 |
| **鉴权** | `corp_id + secret` → `access_token`（2h过期） | `AppID + AppSecret` → `AccessToken` | `token.py` 逻辑简化 |
| **消息加密** | AES-256-CBC + SHA1 验签 | **明文 JSON**（TLS 通道加密） | `crypto.py` 不需要 |
| **消息去重** | 回调重试导致重复 → 需要 DedupCache | WebSocket 可靠传输，基本不重复 | `dedup.py` 不需要 |
| **消息拉取** | 通知+拉取模式 → cursor 分页 | WebSocket 主动推送 | `sync.py` 不需要 |
| **消息发送** | HTTP POST `send_msg` | HTTP POST `/messages` 或 WebSocket | `sender.py` 简化 |
| **SDK** | 无，手写 HTTP 调用 | **官方 `botpy`**（封装 WebSocket + API） | 开发量大幅减少 |
| **启动方式** | `uvicorn` HTTP server | `botpy` 内置 WebSocket client | `uvicorn` 依赖移除 |
| **注册门槛** | 企业微信（需营业执照） | 个人身份证即可 | 开发者友好 |
| **费用** | 免费（不认证） | **完全免费** | — |
| **消息长度限制** | 文本 2048 字符 | 文本 **2000 字符** | `text_splitter.py` 参数调整 |

---

## 二、复用分析

### 2.1 完全复用（无需修改）

| 模块 | 原因 |
|------|------|
| `bridge/agent_bridge.py` | Agent 生命周期管理、SessionState 管理、per-session Lock、惰性过期 — 与信道无关 |
| `bridge/text_splitter.py` | 仅需调整 `max_chars` 默认值从 2000 → 1900（QQ 限制 2000 字符） |
| `cli/_config.py` | 配置查找逻辑完全通用 |
| `cli/wechat.py` → `cli/qqbot.py` | 结构类似，改配置字段名和命令名 |

### 2.2 需要重写

| 微信客服模块 | QQ 机器人对应 | 说明 |
|-------------|-------------|------|
| `channels/wechat_kf/types.py` | `channels/qq_bot/types.py` | 新的 `QQBotConfig`，去掉 crypto/token 字段 |
| `channels/wechat_kf/crypto.py` | **不需要** | QQ 无应用层加密 |
| `channels/wechat_kf/token.py` | `channels/qq_bot/auth.py` | 简单的 `AccessToken` 获取（botpy 内置） |
| `channels/wechat_kf/dedup.py` | **不需要** | WebSocket 不重复推送 |
| `channels/wechat_kf/sync.py` | **不需要** | WebSocket 主动推送 |
| `channels/wechat_kf/sender.py` | `channels/qq_bot/sender.py` | 用 botpy API 发送 |
| `channels/wechat_kf/server.py` | `channels/qq_bot/client.py` | WebSocket 客户端（不是 HTTP server） |
| `channels/wechat_kf/channel.py` | `channels/qq_bot/channel.py` | 重写调度逻辑 |

### 2.3 依赖变化

```diff
- [wechat]: cryptography, starlette, uvicorn
+ [qqbot]:  botpy（官方 SDK，已封装 WebSocket + HTTP API）
```

`httpx` 已是核心依赖，botpy 内部也基于 `httpx`。

---

## 三、QQ 机器人核心机制

### 3.1 通信模型

```
QQ 用户发消息
    │
    ▼
QQ 服务器
    │
    │ WebSocket 推送（长连接，服务端主动推）
    │ 事件类型: C2C_MESSAGE_CREATE（单聊）, GROUP_AT_MESSAGE_CREATE（群聊@）
    ▼
QQBotClient (botpy)
    │
    │ 事件回调中获取消息内容
    │ 直接回复（HTTP API 或 WebSocket）
    ▼
AgentBridge.process()
    │
    ▼
发送回复 (HTTP POST /v2/users/{openid}/messages)
```

**关键优势**：WebSocket 是全双工的，收到消息后可以花任意时间处理并回复，**没有 5 秒超时限制**。

### 3.2 鉴权流程

```
① 获取 AccessToken:
   POST https://bots.qq.com/app/getAppAccessToken
   Body: { "appId": "...", "clientSecret": "..." }
   Response: { "access_token": "...", "expires_in": "7200" }

② WebSocket 连接（botpy 自动处理）:
   wss://api.sgroup.qq.com/websocket
   Header: Authorization: QQBot {access_token}

③ 发送消息:
   POST https://api.sgroup.qq.com/v2/users/{openid}/messages
   Header: Authorization: QQBot {access_token}
   Body: { "content": "回复内容", "msg_type": 0, ... }
```

AccessToken 有效期 2 小时，botpy SDK 自动管理刷新。

### 3.3 消息格式

**接收（WebSocket 推送）：**
```json
{
  "op": 0,
  "t": "C2C_MESSAGE_CREATE",
  "d": {
    "id": "msg_id_xxx",
    "author": { "id": "user_openid", "user_openid": "xxx" },
    "content": "你好",
    "timestamp": "2024-01-01T00:00:00+08:00"
  }
}
```

**发送（HTTP API）：**
```json
POST /v2/users/{openid}/messages
{
  "content": "你好呀",
  "msg_type": 0,
  "msg_id": "optional_unique_id",
  "msg_seq": 1
}
```

### 3.4 支持的消息类型

| 类型 | msg_type | 说明 |
|------|---------|------|
| 文本 | 0 | 纯文本，支持 Markdown |
| 图文混排 | 2 | 图片+文字 |
| Markdown | 2 | Markdown 格式 |
| 富媒体 | 3/4/7 | 图片/视频/语音（需先上传获取 URL） |
| ARK 模板 | 10 | 结构化卡片消息 |
| Embed | 11 | 嵌入式消息 |

---

## 四、整体架构

### 4.1 简化后的分层架构

```
┌──────────────────────────────────────────────────────────┐
│                   CLI 入口                                 │
│           agent-core-qqbot start                          │
│           (新增 console_script 入口点)                      │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────┼──────────────────────────────────┐
│                 Channel Layer (新增)                       │
│                                                          │
│  ┌──────────────────────────────┐                       │
│  │  QQBotClient (botpy.Client)   │  ← WebSocket 客户端   │
│  │  - on_c2c_message_create()   │                       │
│  │  - on_group_at_message()     │                       │
│  └─────────────┬────────────────┘                       │
│                │                                         │
│  ┌─────────────┴────────────────┐                       │
│  │  QQBotChannel                 │  ← 信道核心逻辑        │
│  │  - 事件分发                   │                       │
│  │  - send_message 推送回复      │                       │
│  │  - 连接管理 / 重连            │                       │
│  └─────────────┬────────────────┘                       │
│                │                                         │
│  ┌─────────────┴────────────────┐                       │
│  │  共享模块                     │                       │
│  │  - QQBotConfig               │                       │
│  │  - MessageSender             │                       │
│  └──────────────────────────────┘                       │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────────┐
│                Bridge Layer (复用！)                       │
│                                                          │
│  ┌──────────────────────────────┐                       │
│  │  AgentBridge (未修改)         │                       │
│  │  - session_id → SessionState │                       │
│  │  - Agent(短寿) + stream()    │                       │
│  │  - AgentMessage → 文本收集   │                       │
│  │  - 文本拆分 (1900字符限制)    │                       │
│  │  - 每 session 一个 Lock       │                       │
│  │  - Session TTL 过期清理       │                       │
│  └──────────────────────────────┘                       │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────────┐
│              Agent Core (已有，零改动)                     │
│                                                          │
│  Agent(config).stream(prompt, system=...) → AgentMessage 流│
└──────────────────────────────────────────────────────────┘
```

### 4.2 数据流

```
QQ 用户发消息
    │
    ▼
QQ 服务器 → WebSocket 推送事件
    │
    ▼
botpy.Client.on_c2c_message_create(event)
    │
    │ 提取 user_id, content, msg_type
    │ (botpy 在回调线程中执行，需要用 asyncio.run_coroutine_threadsafe)
    ▼
AgentBridge.process(user_id, channel_id, content, msg_type)
    │
    │  每次新建 Agent(session=SessionState)
    │  agent.stream(content) → AgentMessage 流
    │  收集 assistant_text → 拼接完整回复
    │  按 1900 字符拆分（QQ 限制 2000）
    │
    ▼
botpy.Client.api.post_message(...) → 推送到 QQ
    │
    ▼
用户收到回复
```

---

## 五、模块设计

### 5.1 目录结构

```
src/agent_core/
│
├── channels/                         # 信道层
│   ├── __init__.py
│   ├── qq_bot/                       # 新增 QQ 信道
│   │   ├── __init__.py
│   │   ├── types.py                  # QQBotConfig + 消息常量
│   │   ├── client.py                 # QQBotClient — botpy 封装 + 事件注册
│   │   ├── channel.py                # QQBotChannel — 核心调度
│   │   └── sender.py                 # MessageSender — 消息发送封装
│
├── bridge/                           # 复用，无修改
│   ├── agent_bridge.py               # 已实现，可能需要微调
│   └── text_splitter.py              # 已实现，调整默认 max_chars
│
└── cli/
    ├── __init__.py
    ├── _main.py                      # 无修改
    ├── _config.py                     # 无修改
    ├── wechat.py                     # 已有（微信客服）
    └── qqbot.py                      # 新增：agent-core-qqbot 命令
```

**总共 5 个新文件**（vs 微信客服的 12 个新文件），大幅简化。

### 5.2 核心模块设计

#### 5.2.0 types.py — 配置模型

```python
class QQBotConfig(BaseModel):
    """QQ 机器人接入配置。"""
    app_id: str = Field(..., description="机器人 AppID")
    app_secret: str = Field(..., description="机器人 AppSecret")
    # botpy 自动管理 token，无需手动配置
    
    # Agent 配置（与 WechatKFConfig 相同的 overlay 模式）
    model: str = "anthropic/claude-sonnet-4-5"
    max_turns: int = 20
    permission_mode: PermissionMode = "bypassPermissions"
    system_prompt: str | None = None
    tools: list[str] = ["*"]
    workspace_root: str | None = None
    api_keys: dict[str, str] = {}
    base_urls: dict[str, str] = {}
    
    def to_agent_config(self, base: AgentConfig | None = None) -> AgentConfig:
        # 与 WechatKFConfig.to_agent_config 相同逻辑
        ...
```

#### 5.2.1 client.py — WebSocket 客户端

```python
import asyncio
import botpy
from botpy.types.message import Message

class QQBotClient(botpy.Client):
    """QQ 机器人 WebSocket 客户端。
    
    封装 botpy，注册事件回调，将消息转发给 QQBotChannel。
    """
    
    def __init__(self, config: QQBotConfig, channel: "QQBotChannel"):
        super().__init__(
            bot_app_id=config.app_id,
            bot_token=config.app_secret,  # botpy 用 token 参数名指 secret
            timeout=20,
        )
        self._channel = channel
    
    async def on_c2c_message_create(self, event: Message) -> None:
        """单聊消息回调（botpy 在 asyncio task 中调用）。"""
        await self._channel.handle_message(
            user_id=event.author.user_openid,
            content=event.content,
            msg_type="text",
            msg_id=event.id,
        )
    
    async def on_group_at_message_create(self, event: Message) -> None:
        """群聊 @ 消息回调。"""
        await self._channel.handle_message(
            user_id=event.group_openid,  # 按群 session 隔离
            content=event.content,
            msg_type="text",
            msg_id=event.id,
            group_id=event.group_openid,  # 群聊额外参数
        )
    
    def start(self) -> None:
        """阻塞启动 WebSocket 连接（botpy 内部 event loop）。"""
        botpy.logger.setLevel(logging.WARNING)
        super().start(appid=self._bot_app_id, secret=self._bot_token)
```

#### 5.2.2 channel.py — 核心调度器

```python
class QQBotChannel:
    """QQ 机器人信道调度器。
    
    比 WechatKFChannel 简洁得多——不需要 crypto、token、sync、dedup。
    """
    
    def __init__(self, config: QQBotConfig):
        self._config = config
        self._client = QQBotClient(config, self)
        base_config = _load_global_agent_config()
        self._bridge = AgentBridge(
            config.to_agent_config(base_config),
            system_prompt=config.system_prompt,
        )
    
    async def start(self) -> None:
        """启动：运行 botpy client（阻塞 WebSocket 连接）。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._client.start)
    
    async def stop(self) -> None:
        """botpy 内部管理连接生命周期，此处为占位。"""
        pass
    
    async def handle_message(
        self, user_id: str, content: str, msg_type: str,
        msg_id: str, group_id: str | None = None,
    ) -> None:
        """处理一条用户消息 → Agent 推理 → 回复。
        
        微信客服需要 sync_msg + cursor + dedup。
        QQ 机器人只需直接处理 WebSocket 推送的消息。
        """
        channel_id = group_id or "c2c"
        try:
            replies = await self._bridge.process(
                user_id, channel_id, content, msg_type
            )
        except Exception:
            replies = ["处理出错，请稍后再试。"]
        
        for text in replies:
            await self._send_message(user_id, text, msg_id, group_id)
    
    async def _send_message(
        self, user_id: str, content: str, reply_to_msg_id: str,
        group_id: str | None = None,
    ) -> None:
        """发送回复消息。"""
        # botpy 的 api 属性封装了 HTTP 调用
        await self._client.api.post_message(
            channel_id=user_id,  # 单聊就是 user_openid
            content=content,
            msg_id=str(uuid.uuid4()),
        )
```

#### 5.2.3 sender.py — 消息发送

```python
class MessageSender:
    """QQ 消息发送封装（botpy 的薄封装）。
    
    支持文本、Markdown、图片等多种消息类型。
    """
    
    def __init__(self, api: botpy.API):
        self._api = api
    
    async def send_text(self, user_id: str, content: str) -> None:
        await self._api.post_message(
            channel_id=user_id,
            content=content,
            msg_id=str(uuid.uuid4()),
        )
    
    async def send_markdown(self, user_id: str, template_id: str, params: dict) -> None:
        # QQ 支持 Markdown 模板消息
        ...
    
    async def send_image(self, user_id: str, file_path: str) -> None:
        # 先上传获取 URL，再发送图文消息
        ...
```

#### 5.2.4 cli/qqbot.py — CLI 入口

```python
"""QQ 机器人 CLI 入口 — agent-core-qqbot 命令。

Usage:
    agent-core-qqbot start    # 启动 QQ 机器人
"""

def main() -> None:
    """CLI entry point. Botpy 内部管理自己的 event loop，
    所以直接用 asyncio.run 包装启动逻辑。"""
    config = _load_qqbot_config()
    channel = QQBotChannel(config)
    try:
        asyncio.run(channel.start())
    except KeyboardInterrupt:
        print("[qqbot] stopped")
```

---

## 六、复用与修改总结

### 6.1 无需修改的文件

```
bridge/agent_bridge.py     ← 完全复用
cli/_config.py              ← 完全复用
cli/_main.py                ← 完全复用
```

### 6.2 需要微调的文件

| 文件 | 改动 |
|------|------|
| `bridge/text_splitter.py` | 默认 `max_chars` 从 `2000` → `1900`（QQ 限制 2000 字符，留 100 安全余量） |

### 6.3 新增文件（5 个）

| 文件 | 说明 | 代码量（估） |
|------|------|-----------|
| `channels/qq_bot/__init__.py` | 包标记 | 0 |
| `channels/qq_bot/types.py` | `QQBotConfig` Pydantic 模型 | ~60 行 |
| `channels/qq_bot/client.py` | botpy 事件回调注册 | ~50 行 |
| `channels/qq_bot/channel.py` | 核心调度器 | ~80 行 |
| `channels/qq_bot/sender.py` | 消息发送封装 | ~40 行 |
| `cli/qqbot.py` | CLI 命令 | ~60 行 |

### 6.4 不需要文件（与微信客服对比）

| 微信客服文件 | 原因 |
|-------------|------|
| `crypto.py` | QQ 无应用层加密，TLS 通道 |
| `token.py` | botpy 内置 token 管理 |
| `dedup.py` | WebSocket 可靠传输，不重复推送 |
| `sync.py` | WebSocket 主动推送，无需拉取 |
| `server.py` | botpy 是 WebSocket client，不是 HTTP server |

### 6.5 依赖变化

```toml
[project.scripts]
agent-core = "agent_core.cli:main"
agent-core-wechat = "agent_core.cli.wechat:main"
agent-core-qqbot = "agent_core.cli.qqbot:main"    # 新增

[project.optional-dependencies]
wechat = ["cryptography", "starlette", "uvicorn"]
qqbot = ["botpy"]                                   # 新增
```

---

## 七、实施计划

QQ 机器人接入只需 **一个 Phase**，约 2-3 小时工作量：

| 步骤 | 文件 | 说明 |
|------|------|------|
| 1 | `channels/qq_bot/types.py` | `QQBotConfig` 配置模型（参照已有 `WechatKFConfig`） |
| 2 | `channels/qq_bot/client.py` | botpy 事件回调 + WebSocket 连接 |
| 3 | `channels/qq_bot/channel.py` | `QQBotChannel` 调度器 |
| 4 | `channels/qq_bot/sender.py` | 消息发送封装 |
| 5 | `cli/qqbot.py` | CLI 入口 + 配置加载 |
| 6 | 微调 `bridge/text_splitter.py` | `max_chars` 默认 2000 → 1900 |
| 7 | `pyproject.toml` | 新增入口点 + `[qqbot]` extra |
| 8 | `tests/unit/qq_bot/` | 单元测试 |

### 7.1 测试计划

因为 botpy 提供了完善的 mock 机制，测试比微信客服更简单：

| 测试文件 | 用例数 | 说明 |
|---------|-------|------|
| `test_types.py` | 6 | `QQBotConfig` 验证 + `to_agent_config` merge |
| `test_client.py` | 3 | 事件回调注册 + 消息转发 |
| `test_channel.py` | 5 | 消息处理流水线 + 错误处理 + 重连 |
| `test_sender.py` | 3 | 文本/Markdown/图片发送 |
| **合计** | **~17** | 远少于微信客服的 68 个 |

---

## 八、总结

| | 微信客服 | QQ 机器人 |
|------|---------|----------|
| **新文件数** | 15 个 | **5 个** |
| **代码量（估）** | ~2000 行 | **~300 行** |
| **测试用例** | 68 个 | **~17 个** |
| **依赖** | cryptography + starlette + uvicorn | **botpy**（一个包） |
| **注册门槛** | 企业微信（需营业执照） | **个人身份证** |
| **费用** | 免费（不认证） | **完全免费** |
| **开发调试** | 需要公网 IP/内网穿透 | 本地即可（WebSocket 出站连接） |
| **消息延迟** | 回调+轮询，最多 delay sync_interval | **实时推送** |

核心原因：QQ 机器人有官方 SDK、WebSocket 协议、无应用层加密，实现复杂度仅为微信客服的 **1/4**。

---

## 九、开发注意事项

1. **botpy 的 event loop**：botpy 内部使用自己的 asyncio event loop（`asyncio.run()` 在 `start()` 内部调用）。事件回调在 botpy 的 event loop 中执行，需要注意 asyncio 上下文。
2. **并发安全**：botpy 的事件回调是**串行**的（同一时间只有一个回调在执行），天然避免了同用户并发问题。但 AgentBridge 的 per-session Lock 仍然保留作为安全兜底。
3. **消息长度**：QQ 文本消息限制 2000 字符（不是字节）。当前 `text_splitter.py` 默认 2000，改为 1900 留出安全余量。
4. **群聊 session 隔离**：群聊中按 `group_openid` 建立 session，同一群内所有用户共享对话历史。如需要按用户隔离，可在 `session_id` 中加入 `user_id`。
5. **内网穿透**：本地开发时，WebSocket 是出站连接，不需要公网 IP。但如果需要接收 QQ 服务器的其他 HTTP 回调（如事件订阅），需要配置 IP 白名单。
