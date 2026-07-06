# agent-core 微信客服接入计划书

## 约束条件

- 所有代码在 agent-core 内完全重写，不复制或移植 CowAgent 任何代码
- 仅实现微信客服（WeChat KF），不涉及企业微信应用/机器人等其他信道
- agent-core 核心代码零修改

---

## 一、微信公众号 vs 微信客服：实现原理对比

### 1.1 微信公众号（WeChat Official Account / MP）

微信公众号是最经典的微信服务端接入方式。用户在微信里给公众号发消息，微信服务器把消息转发到你的服务器。

**通信流程：**

```
用户 ──"你好"──▶ 微信客户端 ──▶ 微信服务器
                                      │
                        HTTP POST 到你配置的 URL
                        消息体是 AES 加密的 XML
                                      │
                                      ▼
                              你的服务器（agent-core）
                                      │
                              ← 生成回复 ──
                                      │
                        5 秒内 HTTP Response
                        回复体也是加密 XML
                                      │
                                      ▼
用户 ◀──"你好呀"── 微信客户端 ◀── 微信服务器
```

**技术细节：**

**(1) URL 验证（首次配置时）**

在公众号后台填写服务器 URL 和 Token 后，微信会发一个 GET 请求验证你确实拥有这个服务器：

```
GET /wechat?signature=xxx&echostr=yyy&timestamp=zzz&nonce=www
```

验证算法：
```
sorted_params = sort([token, timestamp, nonce])
hash = sha1("".join(sorted_params))
if hash == signature:
    return echostr   # 验证通过
```

这一步的目的是防止他人随便填写你的 URL。

**(2) 消息加解密（AES-256-CBC）**

微信发来的 POST body 不是明文 XML，而是加密过的：

```xml
<!-- 原始明文 XML -->
<xml>
  <ToUserName><![CDATA[gh_xxx]]></ToUserName>
  <FromUserName><![CDATA[o_xxx]]></FromUserName>
  <CreateTime>1234567890</CreateTime>
  <MsgType><![CDATA[text]]></MsgType>
  <Content><![CDATA[你好]]></Content>
  <MsgId>1234567890123456</MsgId>
</xml>
```

```
加密过程（微信服务器做）：
  明文 XML
  → 前面拼 16 字节随机字符串
  → 后面拼 4 字节网络序的 corp_id 长度 + corp_id
  → PKCS7 填充到 32 字节的倍数
  → AES-256-CBC 加密（key = Base64.decode(encoding_aes_key)，IV = key 前 16 字节）
  → Base64 编码
  → 得到密文字符串

解密过程（你的服务器做）：
  Base64 decode
  → AES-256-CBC 解密
  → PKCS7 unpad
  → 去掉前面 16 字节随机串
  → 从尾部解析出 corp_id 长度，去掉 corp_id
  → 得到明文 XML
```

**注意：EncodingAESKey 是 43 字符的 Base64 字符串，解码后正好是 32 字节（AES-256 密钥）**。

加密后的 POST body：
```xml
<xml>
  <ToUserName><![CDATA[gh_xxx]]></ToUserName>
  <Encrypt><![CDATA[base64密文...]]></Encrypt>
</xml>
```

URL 参数中附带 `msg_signature`、`timestamp`、`nonce`，用于对密文做签名校验：
```
signature = sha1(sort([token, timestamp, nonce, encrypt_text]))
```

**(3) 两种回复模式**

| 模式 | 如何回复 | 限制 | 适用场景 |
|------|---------|------|---------|
| **被动回复** | 在 HTTP Response body 中直接返回加密 XML | **必须在 5 秒内回复**，否则微信断开连接并重试（最多 3 次） | 处理极快的场景（关键词匹配、简单规则） |
| **主动回复** | 先返回空串 `""`，再通过 API `POST https://api.weixin.qq.com/cgi-bin/message/custom/send` 推送 | 需要 access_token | **唯一适合 Agent 的模式**（LLM 推理必然超过 5 秒） |

为什么 Agent 场景必须用主动回复模式：
- LLM 一次推理通常 2-10 秒，加上可能的多轮 tool calling，总耗时轻松超过 30 秒
- 被动模式的 5 秒限制根本不可能满足
- 主动模式下：收到消息→立刻返回空串（微信不重试）→Agent 完成后→API 推送

**(4) access_token 管理**

```
GET https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={app_id}&secret={app_secret}

Response:
{
  "access_token": "xxx",
  "expires_in": 7200   // 2 小时
}
```

access_token 是所有微信 API 调用的通行证，需要定期刷新（过期前 5 分钟刷新）。

**(5) 公众号消息类型**

| MsgType | 说明 |
|---------|------|
| `text` | 文本消息 |
| `image` | 图片消息（含 PicUrl + MediaId） |
| `voice` | 语音消息（含 MediaId + Recognition 文本） |
| `video` | 视频消息 |
| `shortvideo` | 短视频消息 |
| `location` | 地理位置 |
| `link` | 链接 |
| `event` | 事件（关注/取关/菜单点击等） |

---

### 1.2 微信客服（WeChat KF）

微信客服是 2022 年前后推出的新体系，**底层依赖企业微信（WeCom）**，但面向的是**外部微信用户**（不是企业内部员工）。用户在微信公众号/小程序/视频号/支付页面等场景点"联系客服"，就会进入微信客服对话。

**和公众号的本质区别：**

| 维度 | 公众号 | 微信客服 |
|------|--------|---------|
| **所属体系** | 微信公众号平台 (mp.weixin.qq.com) | 企业微信管理后台 (work.weixin.qq.com) |
| **入口** | 用户关注公众号后发消息 | 公众号菜单/小程序/支付页/视频号等挂载客服入口 |
| **消息传递** | **推送模式** — 消息直接在 POST body 里 | **通知+拉取模式** — 先通知你有新消息，你再去拉取 |
| **回复方式** | 被动回复(HTTP Response) 或 客服消息 API | 只能用 send_msg API 主动发送 |
| **API 域名** | `api.weixin.qq.com` | `qyapi.weixin.qq.com`（企业微信域名） |
| **鉴权** | `app_id + app_secret` | `corp_id + secret`（企业微信风格） |
| **会话模型** | 无会话概念，用户发消息就处理 | 有明确的客服会话（session），可转接/结束 |
| **消息去重** | msg_id 去重即可 | cursor 分页 + msg_id 双重保障 |
| **多客服** | 不支持 | 支持多个客服账号（open_kfid），可路由 |

**通信流程：**

```
用户 ──"我要退货"──▶ 微信客户端 ──▶ 微信客服服务器
                                        │
                         ① HTTP POST 回调通知你的服务器：
                            "有新消息，token=xxx"
                            （注意：回调里没有消息内容！）
                                        │
                                        ▼
                                你的服务器（agent-core）
                                        │
                         ② 收到通知后，立刻返回空串 "success"
                            （必须在 5 秒内响应，否则企业微信重试）
                                        │
                         ③ 异步调用 sync_msg API：
                            POST https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg
                            带上 access_token + cursor + token
                            拉取消息详情
                                        │
                                        ▼
                                解析消息 → Agent 推理 → 生成回复
                                        │
                         ④ 调用 send_msg API 推送回复：
                            POST https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg
                            带上 access_token + touser + open_kfid + msgtype + content
                                        │
                              ┌─────────┘
                              ▼
用户 ◀──"好的，请提供订单号"── 微信客户端 ◀── 微信客服服务器
```

**技术细节：**

**(1) 回调通知（步骤①）**

企业微信 POST 到你的回调 URL：

```json
{
  "ToUserName": "wxabcedf",
  "FromUserName": "sys",
  "CreateTime": 1234567890,
  "MsgType": "event",
  "Event": "kf_msg_or_event",
  "Token": "ENCAqRrB..."
}
```

回调也需要加解密（和公众号同样的 AES-256-CBC 方案），只是 XML 换成了 JSON。回调 URL 参数同样带 `msg_signature` 用于验签。

**回调只通知"有消息"，不含消息内容本身**。你需要用 Token 去拉消息。这样设计的原因是：
- 一个回调可能对应多条消息（用户连发多条时批量通知）
- 消息体可能很大（图片/文件），不适合全放在回调里

**(2) 消息同步（步骤③）— sync_msg API**

```
POST https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={token}

Request body:
{
  "cursor": "上次返回的 cursor（首次为空字符串）",
  "token": "从回调中获取的 token",
  "limit": 100,
  "open_kfid": "客服账号 ID（可选）"
}

Response:
{
  "errcode": 0,
  "errmsg": "ok",
  "next_cursor": "4gwCc...",   // 下一页的 cursor，为空表示拉完了
  "has_more": 0,                // 是否还有更多
  "msg_list": [
    {
      "msgid": "msg_xxx",
      "open_kfid": "kf_xxx",
      "external_userid": "wm_xxx",   // 外部用户 ID（发送者）
      "send_time": 1234567890,
      "origin": 3,                    // 3=客户发送，4=客服发送
      "msgtype": "text",
      "text": { "content": "我要退货" }
    }
  ]
}
```

**cursor 机制**：
- cursor 是分页游标，类似数据库的 offset/keyset
- 每次请求带上上次返回的 `next_cursor`
- `next_cursor` 为空或 `has_more=0` 表示当前批拉完
- cursor 需要持久化到本地，避免重启后丢失位置

**首次启动的处理**：刚启动时 cursor 为空，sync_msg 会从头拉取所有历史消息。你需要：
- 首次拉取时设置 `has_more` 标志位，只拉不回复（"排空历史"）
- 记录下历史消息的 msg_id，后续正常处理时对比去重

**(3) 消息发送（步骤④）— send_msg API**

```
POST https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={token}

Request body:
{
  "touser": "wm_xxx",           // 外部用户 ID
  "open_kfid": "kf_xxx",        // 客服账号 ID
  "msgid": "自定义唯一 ID",      // 用于去重，可选
  "msgtype": "text",
  "text": {
    "content": "好的，请提供订单号"
  }
}
```

支持的消息类型：`text`、`image`、`voice`、`video`、`file`、`link`、`miniprogram`、`menu`。

**(4) access_token（企业微信风格）**

```
GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corp_id}&corpsecret={secret}

Response:
{
  "errcode": 0,
  "errmsg": "ok",
  "access_token": "xxx",
  "expires_in": 7200
}
```

同样是 2 小时有效期，需要提前刷新。

---

### 1.3 为什么选择微信客服而非公众号

| 考量 | 公众号 | 微信客服 |
|------|--------|---------|
| **Agent 适配度** | 被动回复有 5s 限制，必须切主动模式 | **天然异步**，无需处理超时切换逻辑 |
| **消息模型** | 推送模式，消息在 POST body | 通知+拉取，更解耦 |
| **会话管理** | 无原生会话概念 | **原生会话模型**（可转接/结束），与 Agent session 天然对应 |
| **入口多样性** | 仅公众号入口 | 公众号/小程序/视频号/支付页等多入口 |
| **多客服** | 不支持 | 支持多客服账号路由 |
| **实现简洁度** | 需处理被动/主动模式切换 | **单一代码路径**，更简洁 |

对于 Agent 场景，微信客服的异步通知+拉取模型天然适合，不用处理 5 秒超时问题。

---

## 二、整体架构

由于只实现微信客服一种信道，架构可以大幅简化——不需要 ChannelManager 管理多信道，不需要 MessageRouter 做复杂路由。

### 2.1 简化后的分层架构

```
┌─────────────────────────────────────────────────────────┐
│                   CLI 入口                                │
│           agent-core-wechat start                        │
│           (独立 console_script 入口点)                     │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────────┐
│                 Channel Layer (新增)                      │
│                                                          │
│  ┌──────────────────────────────┐                       │
│  │  WechatKFServer (starlette)  │  ← HTTP 回调服务器     │
│  │  - GET  /wechat  (URL验证)   │                       │
│  │  - POST /wechat  (回调接收)  │                       │
│  └─────────────┬────────────────┘                       │
│                │                                         │
│  ┌─────────────┴────────────────┐                       │
│  │  WechatKFChannel             │  ← 信道核心逻辑        │
│  │  - 解密回调                   │                       │
│  │  - sync_msg 拉取消息         │                       │
│  │  - send_msg 推送回复         │                       │
│  └─────────────┬────────────────┘                       │
│                │                                         │
│  ┌─────────────┴────────────────┐                       │
│  │  共享模块                     │                       │
│  │  - Crypto (AES-256-CBC)      │                       │
│  │  - TokenManager (access_token)│                      │
│  │  - CursorStore (cursor 持久化)│                      │
│  │  - DedupCache (消息去重)     │                       │
│  └──────────────────────────────┘                       │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────────┐
│                Bridge Layer (新增)                        │
│                                                          │
│  ┌──────────────────────────────┐                       │
│  │  AgentBridge                 │                       │
│  │  - session_id → SessionState │                       │
│  │  - Agent(短寿) + stream()    │                       │
│  │  - AgentMessage → 文本收集   │                       │
│  │  - 文本拆分 (2048字节限制)   │                       │
│  │  - 每 session 一个 Lock（并发安全）│                  │
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

### 2.2 数据流

```
微信用户发消息
    │
    ▼
企业微信服务器
    │
    │ ① POST 回调 (通知 + token)
    ▼
WechatKFServer (starlette)
    │
    │ ② 验签 → 解密 → 过滤事件类型 → 立即返回 "success" (< 100ms)
    │
    │ ③ asyncio.create_task 异步处理
    ▼
TokenManager.get_token() → access_token
    │
    ▼
sync_msg(token, cursor) → msg_list
    │
    │ 对所有 msg_list 中的消息：
    ▼
DedupCache.check(msg_id) → 未处理？
    │
    ▼
构造 IncomingMessage(user_id, content, msg_type)
    │
    ▼
AgentBridge.process(incoming)
    │
    │  每次新建 Agent(session=SessionState)
    │  agent.stream(content) → AgentMessage 流
    │  每个 turn 前 query/loop.py 自动检查 token 预算：
    │    超预算 → 压缩 (compress) 或硬截断 (hard_truncate)
    │  用完立即 agent.aclose()
    │
    ▼
收集所有 assistant_text → 拼接完整回复
    │
    ▼
按 2000 字符拆分长文本 (微信客服 2048 字符限制)
    │
    ▼
send_msg(content) → 推送到微信
    │
    ▼
用户收到回复
```

---

## 三、模块设计

### 3.1 目录结构

```
src/agent_core/
│
├── channels/                         # 信道层
│   ├── __init__.py
│   ├── wechat_kf/
│   │   ├── __init__.py
│   │   ├── server.py                 # HTTP 回调服务器 (starlette 路由)
│   │   ├── channel.py                # WechatKFChannel — 核心调度
│   │   ├── crypto.py                 # AES-256-CBC 加解密 + SHA1 验签
│   │   ├── token.py                  # AsyncTokenManager — access_token 管理
│   │   ├── sync.py                   # sync_msg 拉取 + cursor 持久化
│   │   ├── sender.py                 # send_msg 推送回复
│   │   ├── dedup.py                  # 消息去重 (msg_id LRU)
│   │   └── types.py                  # 微信客服消息类型定义 + WechatKFConfig
│
├── bridge/
│   ├── __init__.py
│   ├── agent_bridge.py               # AgentBridge — session 管理 + Agent 调用
│   └── text_splitter.py              # 字符边界长文本拆分（微信 2048 字符限制）
│
└── cli/
    ├── __init__.py                    # 新增：使 cli 成为 package
    ├── _main.py                      # 原 cli.py 的 main() 逻辑，从 __init__.py 导入
    ├── _config.py                    # 新增：从 _main.py 提取的 _find_config_file / _load_config，供 wechat.py 复用
    └── wechat.py                     # wechat 命令组 (start / status / stop)
```

**CLI 集成方式**：

由于现有 `cli.py` 是单文件模块，不宜直接修改。采用以下方案：
1. 将 `cli.py` 重构为 `cli/` package，保持向后兼容（`from agent_core.cli import main` 继续有效）
2. 将 `_find_config_file()` 和 `_load_config()` 提取到 `cli/_config.py`，供 `_main.py` 和 `wechat.py` 共享——`_main.py` 通过 `from ._config import ...` 引用，保持与原 `cli.py` 一致的行为
3. `cli/wechat.py` 作为独立子模块，定义 `wechat start` 等命令
4. 在 `pyproject.toml` 新增独立入口点：
```toml
[project.scripts]
agent-core = "agent_core.cli:main"
agent-core-wechat = "agent_core.cli.wechat:main"
```

这样微信客服作为**独立 CLI 进程**运行，不侵入现有 REPL 入口，agent-core 核心代码仍然零修改。

总共 **15 个新文件**（含 `cli/__init__.py` 和 `cli/_config.py`），结构清晰，每个文件职责单一。

### 3.2 核心模块设计

#### 3.2.0 types.py — 配置与消息类型

##### 3.2.0.1 配置加载方式

微信客服配置**不引入新的配置文件**，而是在现有的 `agent-core.config.json` 中新增 `wechat_kf` 嵌套字段。这与现有 CLI 的 `_load_config()` 读取同一文件的设计一致，也参考了 CowAgent 将所有信道配置集中在 `config.json` 的做法。

`config.json` 扩展示例：

```json
{
    "model": "deepseek/deepseek-v4-pro",
    "api_keys": {
        "deepseek": "sk-xxx"
    },
    "max_turns": 20,
    "permission_mode": "ask",
    "wechat_kf": {
        "corp_id": "wwxxx",
        "secret": "...",
        "token": "...",
        "encoding_aes_key": "...",
        "open_kfid": "kf_xxx",
        "host": "0.0.0.0",
        "port": 8080,
        "sync_interval": 5.0,
        "model": "deepseek/deepseek-v4-pro",
        "system_prompt": "你是一个客服助手...",
        "permission_mode": "bypassPermissions",
        "tools": ["*"],
        "workspace_root": null,
        "api_keys": {}
    }
}
```

CLI 入口 `cli/wechat.py` 的 `_load_config()` 逻辑：

1. 从 `cli/_config.py` 导入 `_find_config_file()` 查找 `agent-core.config.json`（复用现有查找链：`$AGENT_CORE_CONFIG` → `./agent-core.config.json` → `~/.agent-core/config.json`）
2. 读取顶层字段正常构建 `AgentConfig`（作为 base）
3. 读取 `wechat_kf` 嵌套对象构建 `WechatKFConfig`
4. 调用 `wechat_kf_config.to_agent_config(base)` 得到最终的 Agent 配置

环境变量覆盖：`WECHAT_KF_CORP_ID`、`WECHAT_KF_SECRET`、`WECHAT_KF_TOKEN` 等环境变量可覆盖 `wechat_kf` 中的对应字段，方便 CI/容器化部署。

##### 3.2.0.2 Pydantic 模型

```python
# WechatKFConfig — Pydantic model，与其他 agent-core 配置模型风格一致

from pydantic import BaseModel, Field
from agent_core.models import AgentConfig, PermissionMode

class WechatKFConfig(BaseModel):
    """微信客服接入配置"""
    corp_id: str = Field(..., description="企业 ID")
    secret: str = Field(..., description="应用 secret")
    token: str = Field(..., description="回调 Token（用于签名校验）")
    encoding_aes_key: str = Field(
        ..., min_length=43, max_length=43,
        description="消息加解密密钥（43 字符 Base64）",
    )
    open_kfid: str = Field(..., description="客服账号 ID")
    host: str = Field(default="0.0.0.0", description="HTTP 监听地址")
    port: int = Field(default=8080, ge=1, le=65535, description="HTTP 监听端口")
    sync_interval: float = Field(
        default=5.0, ge=1.0, le=60.0,
        description="主动轮询 sync_msg 的间隔（秒），作为回调的补充",
    )
    # Agent 配置（与 AgentConfig 的字段对应，WechatKFChannel 启动时 merge）
    model: str = Field(default="anthropic/claude-sonnet-4-5")
    max_turns: int = Field(default=20, ge=1, le=500)
    permission_mode: PermissionMode = Field(
        default="bypassPermissions",
        description="微信客服场景必须 bypass，无交互终端可供 ask",
    )
    system_prompt: str | None = Field(
        default=None,
        description="Agent system prompt（客服人设/行为约束）",
    )
    tools: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Agent 可用工具列表，默认为全量。如需限制可显式指定",
    )
    workspace_root: str | None = Field(
        default=None,
        description="文件工具的安全边界目录，None 表示不限制",
    )
    api_keys: dict[str, str] = Field(
        default_factory=dict,
        description="LLM provider -> API key 映射，fallback 到环境变量",
    )
    base_urls: dict[str, str] = Field(
        default_factory=dict,
        description="LLM provider -> 自定义 base URL，覆盖 base 中的同 provider URL",
    )

    def to_agent_config(self, base: AgentConfig | None = None) -> AgentConfig:
        """将 WechatKFConfig 中与 Agent 相关的字段与基础配置合并。
        
        接收一个可选的 base AgentConfig（从 agent-core.config.json 加载），
        对其模型/权限/工具等字段做微信场景 overlay，其余字段（compression、
        logging、sampling、mcp_servers 等）保持 base 的值不变。
        
        若 base 为 None，则从 AgentConfig 默认值构造（适用于纯代码配置方式）。
        """
        cfg = base.model_copy(deep=True) if base else AgentConfig()
        cfg.model = self.model
        cfg.max_turns = self.max_turns
        cfg.permission_mode = self.permission_mode
        cfg.tools = self.tools
        if self.workspace_root is not None:
            cfg.workspace_root = self.workspace_root
        # api_keys / base_urls：微信客服配置中的值覆盖 base 中的同 provider 值
        for provider, key in self.api_keys.items():
            cfg.api_keys[provider] = key
        for provider, url in self.base_urls.items():
            cfg.base_urls[provider] = url
        return cfg
```

**关键设计**：

1. **配置单文件**：微信客服配置从现有的 `agent-core.config.json` 加载（`wechat_kf` 嵌套字段），不新增独立配置文件。CLI 复用现有的 `_find_config_file()` 查找链。

2. **配置分层**：`WechatKFConfig` 管信道参数（corp_id, secret, token 等），`AgentConfig` 管 Agent 行为。`to_agent_config(base)` 是 **merge** 而非凭空创建——先从全局 `agent-core.config.json` 顶层字段加载 `AgentConfig` 作为 base，再用微信客服特有的字段 overlay。**其余字段（compression、logging、sampling、mcp_servers 等）保持 base 的值不变**，微信场景无需重复配置。

3. **`api_keys` 和 `base_urls` 传递**：`WechatKFConfig` 中的 provider→key 和 provider→url 映射会覆盖 base 中的同 provider 值。若 base 中已有对应的 API key/URL，微信客服场景可不再重复配置。

4. **AgentConfig 有 18 个字段**（`models.py`），此方法只 overlay 微信场景需要变动的字段（model、max_turns、permission_mode、tools、workspace_root、api_keys、base_urls），其余全部透传 base。

#### 3.2.1 crypto.py — 加解密

```python
# 职责：AES-256-CBC 加解密 + SHA1 签名校验
# 依赖：cryptography 库（项目新增的 optional extra [wechat] 依赖）

class WechatCrypto:
    """
    微信客服使用与企业微信相同的加解密方案：
    - 算法: AES-256-CBC
    - Key: Base64.decode(43字符 EncodingAESKey) → 32 bytes
    - IV: key 的前 16 bytes
    - Padding: PKCS7
    - 底层实现: cryptography.hazmat.primitives.ciphers.Cipher(algorithms.AES(...), modes.CBC(...))
    """

    def __init__(self, encoding_aes_key: str, token: str, corp_id: str):
        self._aes_key = base64.b64decode(encoding_aes_key + "=")
        self._token = token
        self._corp_id = corp_id

    def verify_signature(
        self, signature: str, timestamp: str, nonce: str, encrypted: str
    ) -> bool:
        """验证回调 URL 参数中的 msg_signature"""
        ...

    def decrypt(self, encrypted_text: str) -> str:
        """
        解密回调中的 Encrypt 字段
        返回 JSON 字符串（企业微信用的是 JSON 而非 XML）
        """
        ...

    def encrypt(self, plain_text: str) -> str:
        """加密回复（URL 验证的 echostr 回传等场景）"""
        ...

    @staticmethod
    def extract_encrypt(raw_body: bytes, content_type: str) -> str:
        """
        从回调 body 中提取 Encrypt 密文字段。
        支持 XML 和 JSON 两种格式，根据 Content-Type 判断。
        """
        ...
```

**加密 body 结构（企业微信）：**

```
[16 bytes random] + [4 bytes msg_len (网络序)] + [JSON plaintext] + [corp_id]
```

**PKCS7 padding 规则：**
```
需要填充 N 个字节 → 每个字节的值都是 N
例如：缺少 3 个字节 → 填充 0x03 0x03 0x03
```

#### 3.2.2 token.py — Token 管理

```python
class AsyncTokenManager:
    """异步 access_token 管理器"""

    def __init__(self, corp_id: str, secret: str, http_client: httpx.AsyncClient):
        self._corp_id = corp_id
        self._secret = secret
        self._http = http_client
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._refresh_failures: int = 0

    async def get_token(self) -> str:
        """
        获取当前有效的 access_token。
        首次调用时获取，后续自动在过期前 5 分钟后台刷新。
        """
        async with self._lock:
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            # 超时或未初始化 → 同步获取
            await self._fetch()
            return self._token

    async def _fetch(self) -> None:
        """调用 gettoken 接口"""
        resp = await self._http.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": self._corp_id, "corpsecret": self._secret},
        )
        data = resp.json()
        if data["errcode"] != 0:
            raise TokenError(data["errmsg"])
        self._token = data["access_token"]
        self._expires_at = time.time() + data["expires_in"]
        self._refresh_failures = 0  # 成功后重置失败计数
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """安排后台刷新（在过期前 5 分钟）。
        
        取消前一个未完成的 refresh task（如有），避免重复刷新。
        """
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        delay = max(0, self._expires_at - time.time() - 300)
        self._refresh_task = asyncio.create_task(
            self._delayed_refresh(delay),
            name="token-refresh",
        )

    async def _delayed_refresh(self, delay: float) -> None:
        """延迟刷新，失败时指数退避重试。
        
        如果后台 refresh task 因网络问题失败，不静默放弃——
        而是退避重试，避免 2 小时后 token 过期导致所有 API 调用失败。
        """
        await asyncio.sleep(delay)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self._fetch()
                return
            except Exception:
                self._refresh_failures += 1
                if attempt < max_retries - 1:
                    backoff = min(60, 5 * (2 ** attempt))
                    logger.warning(
                        f"token refresh failed (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
        logger.error(
            f"token refresh failed after {max_retries} attempts — "
            f"API calls will fail when current token expires"
        )
```

#### 3.2.3 sync.py — 消息同步

```python
class MessageSyncer:
    """sync_msg 消息拉取 + cursor 管理"""

    def __init__(self, token_mgr: AsyncTokenManager, http: httpx.AsyncClient):
        self._token_mgr = token_mgr
        self._http = http
        self._cursor: str = ""          # 初始为空
        self._drained: bool = False     # 是否已完成首次排空
        self._cursor_path: Path = ...   # cursor 持久化文件路径

    async def load_cursor(self) -> None:
        """启动时从文件加载上次的 cursor"""
        ...

    async def save_cursor(self) -> None:
        """原子写入 cursor 到文件 (tmp + os.replace)"""
        ...

    async def fetch_messages(
        self, callback_token: str, open_kfid: str
    ) -> list[dict]:
        """
        调用 sync_msg 拉取新消息。
        首次调用时（_drained=False），只拉不标记为"新消息"，排空历史。
        自动处理 has_more 分页 — 当返回 has_more=1 时循环拉取直到拉完。
        """
        token = await self._token_mgr.get_token()
        all_messages: list[dict] = []
        cursor = self._cursor

        while True:
            resp = await self._http.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={token}",
                json={
                    "cursor": cursor,
                    "token": callback_token,
                    "limit": 100,
                    "open_kfid": open_kfid,
                },
            )
            data = resp.json()

            # 检查 API 错误
            if data.get("errcode", 0) != 0:
                logger.warning(
                    f"sync_msg returned errcode={data['errcode']} "
                    f"errmsg={data.get('errmsg', '')}"
                )
                return []

            all_messages.extend(data.get("msg_list", []))
            cursor = data.get("next_cursor", "")
            self._cursor = cursor

            if data.get("has_more", 0) != 1 or not cursor:
                break

        await self.save_cursor()

        if not self._drained:
            self._drained = True
            return []  # 首批不处理

        return all_messages
```

#### 3.2.4 dedup.py — 消息去重

```python
class DedupCache:
    """
    消息去重缓存。

    微信客服可能因回调重试导致 sync_msg 拉到重复消息。
    维护 msg_id → timestamp 的 TTL + LRU 缓存。
    """

    def __init__(self, ttl_seconds: int = 60, max_size: int = 10_000):
        self._ttl = ttl_seconds
        self._max = max_size
        self._cache: dict[str, float] = {}

    def has_seen(self, msg_id: str) -> bool:
        """检查 msg_id 是否已处理，同时清理过期条目"""
        now = time.time()
        # 清理过期
        expired = [k for k, v in self._cache.items() if now - v > self._ttl]
        for k in expired:
            del self._cache[k]
        return msg_id in self._cache

    def mark_seen(self, msg_id: str) -> None:
        """标记 msg_id 为已处理"""
        if len(self._cache) >= self._max:
            # LRU 淘汰：删除最老的 20%
            cutoff = int(self._max * 0.2)
            sorted_keys = sorted(self._cache, key=self._cache.get)
            for k in sorted_keys[:cutoff]:
                del self._cache[k]
        self._cache[msg_id] = time.time()
```

#### 3.2.5 sender.py — 消息发送

```python
class MessageSender:
    """send_msg API 封装"""

    def __init__(self, token_mgr: AsyncTokenManager, http: httpx.AsyncClient):
        self._token_mgr = token_mgr
        self._http = http

    async def send_text(
        self, touser: str, open_kfid: str, content: str, msgid: str | None = None
    ) -> None:
        token = await self._token_mgr.get_token()
        await self._http.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={token}",
            json={
                "touser": touser,
                "open_kfid": open_kfid,
                "msgid": msgid or str(uuid.uuid4()),
                "msgtype": "text",
                "text": {"content": content},
            },
        )

    async def send_image(self, touser: str, open_kfid: str, media_id: str) -> None:
        """发送图片（需先上传获取 media_id）"""
        ...

    async def upload_media(self, file_path: str, media_type: str) -> str:
        """上传临时素材，返回 media_id"""
        ...
```

#### 3.2.6 agent_bridge.py — 桥接层

```python
import time
import asyncio
from agent_core.session import SessionState

logger = logging.getLogger("agent_core.agent_bridge")

class AgentBridge:
    """连接微信客服信道和 agent-core 的 Agent。

    核心设计原则：
    - SessionState 是长寿的（跨消息持久），存对话历史和权限记忆
    - Agent 是短寿的（每次消息新建，用完即销毁）
    - 这样 Agent 的 anyio cancel-scope 在同 task 内创建和销毁，不跨 task
    - 每个 session 持有 asyncio.Lock，防止同用户并发消息导致
      SessionState.record_turn_result() 全量替换时的竞态丢消息

    上下文管理策略：
    - agent-core 的 query/loop.py 已在每次 LLM 调用前自动执行 token 预算检查
      和上下文压缩（compress → hard_truncate），AgentBridge 无需重复实现
    - 会话惰性过期：仅在 process() 入口处检查，超时未活动的 session 直接移除
      借鉴 CowAgent 的 ExpiredDict 惰性过期模式，无需后台 task
    - 被移除的 session 中丢失的对话记忆由微信用户自行发起新对话补偿
      （微信客服状态栏有"结束会话"按钮，用户主动结束也可触发清理）
    """

    def __init__(
        self,
        base_config: AgentConfig,
        system_prompt: str | None = None,
        session_timeout: float = 3600.0,
    ):
        self._base = base_config
        self._system_prompt = system_prompt
        self._session_timeout = session_timeout  # 1 小时无活动自动移除
        # 每个微信用户一个 SessionState（不是 Agent 实例！）
        self._sessions: dict[str, SessionState] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 惰性过期：记录最后访问时间，仅 process() 入口处检查
        self._sessions_last_access: dict[str, float] = {}

    @staticmethod
    def session_id(touser: str, open_kfid: str) -> str:
        """生成会话 ID"""
        return f"{open_kfid}:{touser}"

    def _get_or_create_session(self, session_id: str) -> SessionState:
        """获取或创建 SessionState，同时惰性清理超时 session。

        惰性过期（借鉴 CowAgent ExpiredDict）：仅在访问时检查并清理，
        不需要后台 task，避免了计划外并发和取消传播的复杂性。
        """
        now = time.time()
        # 惰性清理当前 session_id 对应的旧会话（如果超时）
        if session_id in self._sessions:
            last = self._sessions_last_access.get(session_id, 0.0)
            if now - last > self._session_timeout:
                self._sessions.pop(session_id, None)
                self._session_locks.pop(session_id, None)
                self._sessions_last_access.pop(session_id, None)
                logger.info(f"Session {session_id[:16]}... expired and removed")

        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState()
            self._session_locks[session_id] = asyncio.Lock()
        self._sessions_last_access[session_id] = now
        return self._sessions[session_id]

    async def process(
        self, user_id: str, open_kfid: str, content: str, msg_type: str
    ) -> list[str]:
        """
        处理一条用户消息，返回要发送的文本片段列表（已拆分）。

        每次消息新建 Agent，用完立即 aclose()，保证 anyio cancel-scope
        在同一个 asyncio task 内创建和销毁。

        如果 msg_type 是 image/voice，content 可以带上媒体描述：
          image → "[图片消息]"
          voice → "[语音消息]: 识别文本（如果有）"

        并发安全：同用户的并发消息通过 per-session Lock 串行化，
        防止两个 Agent 同时写 SessionState 导致历史丢失。

        Agent.stream() 内部自动管理 SessionState 的历史回写和权限记忆：
        - 通过 session.snapshot_for_next_turn() 注入对话历史（agent.py:169）
        - 通过 session.permission_store 记住 tool 权限决策（agent.py:171）
        - 收到 done 消息时自动调用 session.flush_new_messages() 和
          session.record_turn_result() 回写历史（agent.py:185-187）
        - query/loop.py 在每个 turn 前自动检查 token 预算，必要时触发
          上下文压缩（compress → CompactMessage 事件）或硬截断
        AgentBridge 无需手动调用这些方法，只需持有 SessionState 传入 Agent 即可。
        """
        sid = self.session_id(user_id, open_kfid)
        session = self._get_or_create_session(sid)
        lock = self._session_locks[sid]

        async with lock:
            prompt = self._build_prompt(content, msg_type)

            # 每次消息新建 Agent，共享同一个 SessionState
            agent = Agent(self._base, session=session)

            try:
                collected: list[str] = []
                error_msg: str | None = None
                async for msg in agent.stream(
                    prompt, system=self._system_prompt
                ):
                    if msg.type == "assistant_text":
                        collected.append(msg.content)
                    elif msg.type == "error":
                        error_msg = msg.message
                        logger.error(f"Agent error: {error_msg}")

                # 优先返回收集到的文本（即使有错误，部分回复也好于无回复）
                full_reply = "".join(collected)
                if not full_reply.strip():
                    if error_msg:
                        return ["抱歉，处理您的请求时遇到了问题，请稍后再试。"]
                    return []  # 纯 tool_use 或无文本回复，不发送空消息
                return self._split_text(full_reply)
            finally:
                await agent.aclose()

    @staticmethod
    def _split_text(text: str, max_chars: int = 2000) -> list[str]:
        """按字符边界拆分长文本，确保每条不超过微信客服 2048 字符限制。
        
        微信客服 send_msg API 的 text.content 有 2048 个字符的限制（不是字节）。
        使用 2000 留出安全余量。
        """
        if len(text) <= max_chars:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            # Try to break at a natural boundary (newline or space)
            if end < len(text):
                # Search backwards for a natural break within last 200 chars
                search_start = max(start, end - 200)
                for sep in ("\n", "。", ". ", " "):
                    pos = text.rfind(sep, search_start, end)
                    if pos > search_start:
                        end = pos + len(sep) if sep.endswith(" ") else pos + 1
                        break
            chunks.append(text[start:end].strip())
            start = end
        return chunks

    def clear_session(self, user_id: str, open_kfid: str) -> None:
        """用户发送 /reset 或结束客服会话时清除会话"""
        sid = self.session_id(user_id, open_kfid)
        self._sessions.pop(sid, None)
        self._session_locks.pop(sid, None)
        self._sessions_last_access.pop(sid, None)

    def _build_prompt(self, content: str, msg_type: str) -> str:
        """构建 Agent 输入 prompt。
        
        文本消息直接透传；语音消息优先使用微信语音识别的 Recognition 文本；
        图片等其他类型附上媒体描述供 LLM 理解上下文。
        """
        if msg_type == "text":
            return content
        if msg_type == "voice":
            return f"[语音消息] {content}"
        if msg_type == "image":
            return f"[图片消息] {content}"
        return f"[{msg_type}消息] {content}"

```

#### 3.2.7 channel.py — 核心调度

```python
import logging
import traceback
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("agent_core.wechat_kf")

class WechatKFChannel:
    """
    微信客服信道的核心调度器。
    将 server.py（HTTP）、crypto、token、sync、sender、dedup、bridge
    串联起来，完成完整的消息处理流水线。

    关键约束：Agent 的 anyio cancel-scope 要求 Agent 的创建、
    stream() 消费、aclose() 必须在同一个 asyncio task 中完成。
    因此 _process() 内部完整管理 Agent 生命周期，不跨 task 传递 Agent。

    优雅关闭：维护 _pending_tasks 集合追踪所有在途的 _process task，
    stop() 时等待所有 task 完成（最多 30s 超时），超时后 cancel 剩余 task。
    """

    def __init__(self, config: WechatKFConfig):
        self._config = config
        self._http = httpx.AsyncClient(timeout=30)
        self._crypto = WechatCrypto(
            config.encoding_aes_key, config.token, config.corp_id
        )
        self._token_mgr = AsyncTokenManager(
            config.corp_id, config.secret, self._http
        )
        self._syncer = MessageSyncer(self._token_mgr, self._http)
        self._sender = MessageSender(self._token_mgr, self._http)
        self._dedup = DedupCache()
        # 从全局 agent-core.config.json 加载基础 AgentConfig，再用
        # WechatKFConfig 的 Agent 相关字段 overlay
        base_agent_config = _load_global_agent_config()
        self._bridge = AgentBridge(
            config.to_agent_config(base_agent_config),
            system_prompt=config.system_prompt,
        )
        self._pending_tasks: set[asyncio.Task] = set()
        self._shutdown_event = asyncio.Event()
        # 保存最近一次回调的 token，供轮询时使用
        self._last_callback_token: str = ""

    async def start(self) -> None:
        """启动：加载 cursor → 启动后台轮询 → 启动 HTTP server"""
        await self._syncer.load_cursor()
        # 启动定时轮询 loop，作为回调的补充（处理回调丢失的情况）
        poll_task = asyncio.create_task(
            self._polling_loop(), name="wechat-kf-poll"
        )
        self._pending_tasks.add(poll_task)
        try:
            app = self._build_starlette_app()
            config = uvicorn.Config(
                app, host=self._config.host, port=self._config.port
            )
            server = uvicorn.Server(config)
            await server.serve()
        finally:
            self._shutdown_event.set()
            poll_task.cancel()

    async def stop(self) -> None:
        """优雅关闭：等待在途 task 完成，超时后强制取消。"""
        self._shutdown_event.set()
        if self._pending_tasks:
            # 等待所有 task 完成，最多 30s
            done, pending = await asyncio.wait(
                self._pending_tasks, timeout=30.0
            )
            for task in pending:
                task.cancel()
        await self._http.aclose()

    async def _polling_loop(self) -> None:
        """后台定时轮询 sync_msg，作为回调的补充。
        
        微信回调可能因网络抖动丢失。定时轮询确保即使回调未到达，
        也能在 sync_interval 秒内拉取到新消息。
        """
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._config.sync_interval)
                # 使用最近一次回调的 token 拉取消息（若无回调则为空字符串）
                await self._process_poll()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning(
                    f"polling sync_msg failed:\n{traceback.format_exc()}"
                )

    async def _process_poll(self) -> None:
        """轮询触发的消息拉取。与回调驱动的 _process 逻辑相同。
        
        sync_msg API 的 token 参数为必填。轮询时使用最近一次回调中获取的 token；
        若尚未收到任何回调，传空字符串（此时仅按 cursor 拉取，首次排空模式下可行）。
        """
        await self._process_safe(self._last_callback_token)

    def _build_starlette_app(self) -> Starlette:
        routes = [
            Route("/wechat", self._handle_callback, methods=["GET", "POST"]),
        ]
        return Starlette(routes=routes)

    async def _handle_callback(self, request: Request) -> Response:
        """处理企业微信回调"""
        if request.method == "GET":
            return await self._verify_url(request)

        # POST 回调
        body = await request.body()
        content_type = request.headers.get("content-type", "")
        params = request.query_params

        # 1. 提取 Encrypt 密文字段（需先提取才能验签）
        try:
            encrypted = WechatCrypto.extract_encrypt(body, content_type)
        except Exception:
            return Response("success")  # 无法提取密文，返回 success 避免重试

        # 2. 验签 — 计算对象是 Encrypt 字段值，不是 raw body
        #    签名算法: SHA1(sort([token, timestamp, nonce, encrypt]))
        msg_signature = params.get("msg_signature", "")
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")
        if not msg_signature or not timestamp or not nonce:
            return Response("success")
        if not self._crypto.verify_signature(
            msg_signature, timestamp, nonce, encrypted
        ):
            return Response("", status_code=403)

        # 3. 解密 Encrypt → 明文 JSON
        try:
            json_str = self._crypto.decrypt(encrypted)
            event = json.loads(json_str)
        except Exception:
            return Response("success")  # 解密失败也要返回 success（协议要求）

        # 4. 过滤事件类型：仅处理 kf_msg_or_event 事件，忽略 suite_ticket
        #    等其他回调类型（否则可能误处理导致 Token 提取失败或空转）
        if event.get("MsgType") != "event" or event.get("Event") != "kf_msg_or_event":
            logger.debug(f"ignoring non-kf callback: MsgType={event.get('MsgType')}, Event={event.get('Event')}")
            return Response("success")

        # 5. 提取 Token 并保存（供轮询兜底使用）
        callback_token = event.get("Token")
        if not callback_token:
            return Response("success")
        self._last_callback_token = callback_token

        # 6. 异步处理：创建独立 task，在 task 内闭环 Agent 的完整生命周期。
        #    同时用 _track_task 追踪 task，便于 stop() 时等待。
        task = asyncio.create_task(
            self._process_safe(callback_token),
            name=f"wechat-kf-{callback_token[:8]}",
        )
        self._track_task(task)

        # 7. 立即返回（必须 < 5s）
        return Response("success")

    def _track_task(self, task: asyncio.Task) -> None:
        """追踪 task，完成后自动从 _pending_tasks 移除。"""
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _process_safe(self, callback_token: str) -> None:
        """带异常捕获的 _process 包装，防止 create_task 异常被丢弃。
        同时捕获 CancelledError，避免关闭时的取消导致未处理异常。"""
        try:
            await self._process(callback_token)
        except asyncio.CancelledError:
            logger.info(f"_process cancelled for token {callback_token[:8]}")
        except Exception:
            logger.error(
                f"unhandled error in _process:\n{traceback.format_exc()}"
            )

    async def _process(self, callback_token: str) -> None:
        """异步消息处理主流程。

        Agent 的创建、stream、aclose 全在这个 task 内闭环，
        满足 anyio cancel-scope 的同 task 约束。
        """
        try:
            messages = await self._syncer.fetch_messages(
                callback_token, self._config.open_kfid
            )
        except Exception:
            return

        for msg in messages:
            if msg["origin"] != 3:  # origin=3 才是客户发的
                continue
            uid = msg["external_userid"]
            msg_id = msg["msgid"]
            if self._dedup.has_seen(msg_id):
                continue

            self._dedup.mark_seen(msg_id)

            content = self._extract_content(msg)
            msg_type = msg["msgtype"]

            try:
                replies = await self._bridge.process(
                    uid, self._config.open_kfid, content, msg_type
                )
            except Exception:
                logger.error(
                    f"AgentBridge.process error for user {uid}:\n"
                    f"{traceback.format_exc()}"
                )
                replies = ["处理出错，请稍后再试。"]

            for text in replies:
                await self._sender.send_text(
                    uid, self._config.open_kfid, text
                )


def _load_global_agent_config() -> AgentConfig | None:
    """从 agent-core.config.json 加载基础 AgentConfig。
    
    微信客服场景的 Agent 行为应基于用户已有的全局配置（模型、API key 等），
    仅对 permission_mode 等特定字段做微信客服 overlay。
    若配置文件不存在或解析失败，返回 None（WechatKFConfig.to_agent_config()
    会使用 AgentConfig 默认值作为 fallback）。
    """
    ...
```

---

## 四、实施计划

### Phase 1：基础模块

| 步骤 | 文件 | 说明 |
|------|------|------|
| 1.1 | `channels/wechat_kf/types.py` | 消息类型定义（MsgType 枚举、消息 dict → IncomingMessage 转换） |
| 1.2 | `channels/wechat_kf/crypto.py` | AES-256-CBC 加解密 + SHA1 验签 |
| 1.3 | `channels/wechat_kf/token.py` | AsyncTokenManager — access_token 获取与自动刷新 |
| 1.4 | `channels/wechat_kf/dedup.py` | DedupCache — msg_id 去重 |

### Phase 2：信道核心

| 步骤 | 文件 | 说明 |
|------|------|------|
| 2.1 | `channels/wechat_kf/sync.py` | MessageSyncer — sync_msg 拉取 + cursor 持久化 + 首次排空 |
| 2.2 | `channels/wechat_kf/sender.py` | MessageSender — send_msg 文本/图片发送 |
| 2.3 | `channels/wechat_kf/server.py` | starlette HTTP 路由（GET URL验证 / POST 回调） |
| 2.4 | `channels/wechat_kf/channel.py` | WechatKFChannel — 串联所有模块的调度器 |

### Phase 3：桥接层

| 步骤 | 文件 | 说明 |
|------|------|------|
| 3.1 | `bridge/text_splitter.py` | 字符边界安全拆分，2000 字符/条（微信客服限制 2048 字符） |
| 3.2 | `bridge/agent_bridge.py` | AgentBridge — session 管理 + `.stream()` 调用 + 文本收集 |

### Phase 4：集成

| 步骤 | 文件 | 说明 |
|------|------|------|
| 4.1 | `cli/__init__.py` + `cli/_main.py` + `cli/_config.py` | 将现有 `cli.py` 重构为 package：`_config.py` 提取 `_find_config_file()` 和 `_load_config()` 供共享；`_main.py` 保留 REPL 逻辑；`__init__.py` 重导出 `main`（保持 `from agent_core.cli import main` 兼容） |
| 4.2 | `cli/wechat.py` | CLI 命令组 (`start` / `status` / `stop`)，独立入口点 `agent-core-wechat`，从 `cli/_config.py` 导入 `_find_config_file` 和 `_load_config` |
| 4.3 | `pyproject.toml` | 新增 `agent-core-wechat` 入口点；新增 `[wechat]` optional extra（`cryptography` 用于 AES-256-CBC 加解密，`starlette` + `uvicorn` 用于 HTTP 回调服务器）。`httpx` 已是核心依赖 |

---

## 五、关键技术要点

### 5.1 EncodingAESKey 的长度

`EncodingAESKey` 是 43 字符的 Base64 字符串。Base64 每 4 个字符编码 3 个字节，43 字符实际上只能解码出 32 个字节（43 = 32*8/6 + 1 填充字符被省略了）。所以：
```python
key = base64.b64decode(encoding_aes_key + "=")  # 补上填充 → 32 bytes
```

### 5.2 验签用 Encrypt 字段值，非整个 body（重要）

回调验证签名时，hash 的输入是**从 POST body 中提取出来的 Encrypt 字段值**，不是整个原始 body。官方加解密库 `WXBizMsgCrypt.py` 的签名计算逻辑：

```python
# SHA1.getSHA1(token, timestamp, nonce, encrypt):
sortlist = [token, timestamp, nonce, encrypt]  # encrypt = 从 body 中提取的密文
sortlist.sort()
sha = hashlib.sha1()
sha.update("".join(sortlist))
return sha.hexdigest()
```

**正确的回调处理顺序**：
```
1. 从 body 中提取 Encrypt 字段值
2. 用 [token, timestamp, nonce, encrypt] 排序拼接计算 SHA1，比对 msg_signature
3. 验签通过后，用 AES-256-CBC 解密 Encrypt 得到明文 JSON
```

**错误做法**：直接对 raw body 做签名校验（body 包含 ToUserName 等外层字段，与签名计算无关）。

企业微信回调 POST body 可以是 XML 或 JSON 格式。微信客服（KF）建议使用 JSON 格式：
```xml
<!-- XML 格式 -->
<xml>
  <ToUserName><![CDATA[...]]></ToUserName>
  <Encrypt><![CDATA[base64_ciphertext...]]></Encrypt>
</xml>
```
```json
// JSON 格式
{
  "ToUserName": "...",
  "Encrypt": "base64_ciphertext..."
}
```
需要从中提取 `Encrypt` 字段，解密后得到 JSON（报文明文内容）。需同时支持两种格式，根据 Content-Type 判断。

### 5.3 回调 URL 验证

GET 请求的验证算法：
```python
echostr = request.query_params["echostr"]  # URL 参数名是 echostr
sorted_params = sorted([token, timestamp, nonce, echostr])
hash = hashlib.sha1("".join(sorted_params).encode()).hexdigest()
if hash == msg_signature:
    # 解密 echostr 并原样返回明文
    plain = crypto.decrypt(echostr)
    return Response(plain)
```

### 5.4 首次启动的"排空历史"

首次启动时 cursor 为空，sync_msg 会返回所有历史消息。需要先跑一轮 sync_msg 把 cursor 推到最新位置，**但不处理这些历史消息**。实现：
- `_drained` 标志位，初始为 `False`
- 第一次 sync 只更新 cursor，不返回消息
- 之后的 sync 正常返回待处理消息

### 5.5 错误处理

| 场景 | 处理 |
|------|------|
| sync_msg 返回 errcode != 0 | 记录日志 + errcode/errmsg，返回空列表放弃本次处理（微信会重试回调） |
| sync_msg 返回 has_more=1 | 循环拉取直到 has_more=0 或 next_cursor 为空，防止漏消息 |
| Agent 推理异常 | 发送"处理出错，请稍后再试"，记录完整 traceback |
| LLM API 返回 error（AgentMessage type="error"） | 优先发送已收集到的部分文本回复（若有），否则发送通用错误提示 |
| send_msg 失败 | 记录日志（微信不会重试，消息丢失） |
| 解密失败 | 返回 "success"，避免企业微信重复回调 |
| access_token 过期（errcode 42001） | 强制刷新 token 后重试一次 |
| Agent 回复为空（纯 tool_use / 无文本） | 不发送消息（非错误），避免向微信推送空内容 |
| `asyncio.create_task` 后台 task 未捕获异常 | 通过 `_process_safe()` 包装，捕获异常并 log.error（否则 asyncio 仅打印警告到 stderr，无人察觉） |
| TokenManager 后台刷新 task 网络失败 | 指数退避重试（最多 3 次），失败后 log.error 告警（而非静默过期） |
| uvicorn 关闭时 in-flight task | `stop()` 用 `asyncio.wait(_pending_tasks, timeout=30)` 等待完成；超时后 cancel 剩余 task；`_process_safe` 捕获 `CancelledError` 避免误报 |
| 后台定时轮询 sync_msg 失败 | 记录 warning 日志，下一轮继续（不中断 polling loop） |

### 5.5.1 权限模式配置（重要）

微信客服场景**不存在交互终端**，无法在运行时询问用户是否允许 tool call。`PermissionEngine._ask()` 在无 callback 时的实际行为（`engine.py:199-214`）：

```python
# No callback available
if tool_is_destructive:
    return PermissionDecision(allowed=False, ...)  # 立即拒绝，不阻塞
# Non-destructive, no callback: allow by default
return PermissionDecision(allowed=True, ...)
```

注意：`_ask()` 在无 callback 时**不会阻塞**——它对 destructive 工具立即拒绝，对 non-destructive 工具立即允许。

三种模式的完整行为分析（`engine.py` 匹配链 step 5→6→7）：

| 模式 | 只读工具 | 非只读、非破坏性工具 | 破坏性工具（Edit/Write/Bash 等） |
|------|---------|---------------------|--------------------------------|
| `bypassPermissions` | step 5 直接允许 | step 5 直接允许 | step 5 直接允许 |
| `acceptEdits` | step 5 直接允许 | step 5 直接允许 | 落入 step 7 → 无 callback → 立即拒绝 |
| `ask` | step 6（只读兜底）允许 | 落入 step 7 → 无 callback → 立即允许 | 落入 step 7 → 无 callback → 立即拒绝 |

因此 `"ask"` 模式在无 callback 时**不会挂起**，其行为与 `"acceptEdits"` 几乎一致（破坏性工具被拒绝，其他工具被允许）。两者都不适合微信客服场景的核心原因是：**所有写操作都会被拒绝**。

结论：
- `WechatKFConfig.permission_mode` **默认值必须为 `"bypassPermissions"`**
- `"acceptEdits"` 和 `"ask"` 在无 callback 时都不会阻塞，但都会拒绝所有破坏性工具，导致 Agent 的写操作（编辑文件、执行 Bash 等）全部失败
- 如果需要限制写工具权限，应在 `WechatKFConfig.tools` 列表中直接过滤（如 `tools: ["file_read", "glob_tool", "grep_tool", "web_search", "web_fetch"]`），然后使用 `bypassPermissions`

### 5.6 并发安全：SessionState 竞态

同一微信用户快速连发两条消息时，HTTP 回调会触发两个并发的 `_process` task。它们共享同一个 `SessionState`，而 `SessionState.record_turn_result()` 是**全量替换** `messages` 列表（`self.messages = list(final_messages)`）。若不加以控制：

```
Task A: stream(msg1) → DoneMessage(final_messages=[msg1, reply1])
Task B: stream(msg2) → DoneMessage(final_messages=[msg1, msg2, reply2])
Task A 先完成 → session.messages = [msg1, reply1]
Task B 后完成 → session.messages = [msg1, msg2, reply2]  ← msg1 的回复丢失
```

**解决方案**：`AgentBridge` 中每个 session_id 持有一个 `asyncio.Lock`，`process()` 在 `async with lock` 内执行。同用户的消息被串行化，不同用户之间不受影响。

### 5.7 anyio cancel-scope 约束（重要）

agent-core 的 `Agent.stream()` 内部使用 anyio cancel scope 管理 MCP session。该约束要求 **MCP session 的创建和销毁必须在同一个 asyncio task 中完成**（源码：`query/loop.py` 的 `query()` 函数用 `try/finally` 包裹 `McpSessionManager`，保证 `mcp.aclose()` 在创建时的同一 task 中调用；`agent.py` 的 `stream()` 方法用 `aclosing()` 包裹 `query()` 生成器）。

违反此约束（例如在 task A 创建 Agent/stream，在 task B 关闭）会抛出：
```
RuntimeError: Attempted to exit cancel scope in a different task
```

**本项目中的应对**：
1. Agent 实例**每次消息新建，用完立即 aclose()**，在同一个 `_process` task 内闭环
2. AgentBridge 维护 `SessionState`（长寿）而非 `Agent`（短寿），确保不跨 task 传递 Agent
3. HTTP handler 用 `asyncio.create_task` 创建独立的 `_process` task，Agent 的完整生命周期在这个 task 内完成
4. 优雅关闭时 `stop()` 等待所有 `_pending_tasks` 完成（最多 30s），避免 task 被意外取消导致 MCP session 泄露

### 5.8 Agent 实例级别的并发安全

`Agent` 实例不是并发安全的（实例级状态：`_recorder`, `_sink`, `_trace_id`）。本方案通过"每次消息新建 Agent"保证了并发安全——同用户多消息或不同用户多消息各自创建独立 Agent 实例，不共享可变状态。同一用户的消息并发由 `AgentBridge` 的 per-session Lock 串行化（见 5.6 节）。

---

## 六、总结

本方案聚焦微信客服一种信道，整体实现 **15 个文件**：

- `channels/wechat_kf/` 下 8 个文件：类型+配置、加解密、token、去重、同步、发送、server、调度器
- `bridge/` 下 2 个文件：文本拆分、Agent 桥接（SessionState 管理 + Agent 生命周期）
- `cli/` 下 4 个文件：package 初始化、配置加载（提取 `_find_config_file` 和 `_load_config` 供共享）、原 CLI 逻辑迁移、wechat 命令组
- 配置扩展（`pyproject.toml` 入口点 + optional extra）

核心设计原则：
- **SessionState 长寿，Agent 短寿**：记忆在 SessionState 中持久，Agent 每次消息新建用完即销毁
- **Agent.stream() 内置上下文管理**：history 回写（`record_turn_result`）、持久化（`flush_new_messages`）、权限记忆（`permission_store`）均由 Agent 内部自动处理；token 预算检查和上下文压缩（compress → hard_truncate）由 `query/loop.py` 在每个 turn 前自动执行。AgentBridge 无需重复实现压缩逻辑
- **会话惰性过期**：借鉴 CowAgent 的 `ExpiredDict` 惰性过期模式，仅在 `process()` 入口处检查并清理超时 session，无需后台 task
- **配置单文件**：微信客服配置从现有 `agent-core.config.json` 的 `wechat_kf` 嵌套字段加载（不新增独立配置文件）。`_find_config_file()` 和 `_load_config()` 从 `cli/_config.py` 提取为共享模块，供 `_main.py`（REPL）和 `wechat.py`（微信客服）共同使用
- **配置分层 merge**：`WechatKFConfig.to_agent_config(base)` 从全局 `agent-core.config.json` 顶层字段加载基础 AgentConfig，仅对微信场景的字段（model、max_turns、permission_mode、tools、workspace_root、api_keys、base_urls）overlay，其余透传
- **anyio cancel-scope 约束**：Agent 创建/消费/关闭在同一个 asyncio task 内闭环
- **agent-core 核心代码零修改**
- **权限模式默认 `bypassPermissions`**：微信客服无交互终端。`"ask"` 和 `"acceptEdits"` 在无 callback 时均不会阻塞，但都会拒绝所有破坏性工具（写操作全部失败），不推荐。详见 5.5.1 节行为分析表
- **per-session Lock 防并发竞态**：同用户并发消息串行化，防止 SessionState 历史被覆盖
- **事件类型过滤**：回调 handler 检查 `MsgType == "event"` 且 `Event == "kf_msg_or_event"`，忽略 suite_ticket 等其他企业微信回调
- **优雅关闭**：追踪所有 in-flight _process task，stop() 等待最多 30s 后 cancel 剩余 task
- **回调 + 定时轮询双保险**：回调驱动为主，后台 polling loop 兜底（回调丢失时确保 sync_interval 内拉取到新消息）
- **独立 CLI 入口点**：`agent-core-wechat start`，不侵入现有 `agent-core` 命令
- **cryptography 库实现 AES-256-CBC**：企业微信加解密采用 `cryptography` 库（`[wechat]` optional extra），非自实现

微信客服的异步通知+拉取模型天然适合 Agent 场景，不需要处理 5 秒超时问题，实现比公众号简洁。


---

## 七、测试计划

### 7.1 测试基础设施

复用项目现有测试框架：

| 组件 | 选型 |
|------|------|
| 测试框架 | pytest ≥ 9.0.3 + pytest-asyncio ≥ 1.4.0 |
| HTTP Mock | `respx`（httpx 官方推荐的 mocking 库，项目已用 httpx） |
| LLM Mock | 复用 `tests/integration/mock_model.py` 的 `MockClient` + `make_text_turn()` 等工厂 |
| 配置 Fixture | 复用 `tests/conftest.py` 的 `default_config` / `custom_config` |
| 文件 I/O | `tmp_path`（pytest 内置） |
| 异步支持 | `pytest.mark.asyncio` + `asyncio.run()` 辅助 `_collect()` |

新增测试依赖（`pyproject.toml` `[wechat]` extra 或 dev dependency）：

```toml
[project.optional-dependencies]
wechat = ["cryptography", "starlette", "uvicorn"]
test = ["pytest>=9.0.3", "pytest-asyncio>=1.4.0", "vcrpy>=7", "respx>=0.22"]
```

### 7.2 测试目录结构

```
tests/
├── unit/
│   └── wechat_kf/
│       ├── __init__.py
│       ├── conftest.py              # 共享 fixtures: sample WechatKFConfig, mock HTTP responses
│       ├── test_crypto.py           # AES-256-CBC 加解密 + SHA1 验签 + extract_encrypt
│       ├── test_token.py            # AsyncTokenManager 获取/缓存/刷新/重试
│       ├── test_dedup.py            # DedupCache 去重/TTL/LRU
│       ├── test_sync.py             # MessageSyncer cursor/排空/分页
│       ├── test_sender.py           # MessageSender send_text/send_image
│       ├── test_types.py            # WechatKFConfig 校验 + to_agent_config merge
│       ├── test_text_splitter.py    # AgentBridge._split_text 字符边界拆分
│       └── test_agent_bridge.py     # AgentBridge session/Agent 生命周期/并发/Lock
│
├── integration/
│   └── wechat_kf/
│       ├── __init__.py
│       ├── conftest.py              # mock_agent_client fixture（MockClient 注入）
│       ├── test_channel_pipeline.py # 完整回调→解密→同步→Agent→发送 流水线
│       ├── test_channel_lifecycle.py# start/stop/优雅关闭/polling loop
│       ├── test_error_scenarios.py  # sync_msg 失败 / Agent 报错 / send_msg 失败
│       └── test_concurrency.py      # 同用户并发消息 → Lock 串行化
```

### 7.3 单元测试用例

#### 7.3.1 `test_crypto.py` — 加解密

复用企业微信官方加解密库 `WXBizMsgCrypt.py` 的已知测试向量（固定 AES key + token + corp_id + 明文/密文对）作为参数化用例。

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_decrypt_known_vector` | 已知密文 + 正确 key/token/corp_id | 解密得到已知明文 JSON |
| `test_encrypt_roundtrip` | 任意 JSON 明文 → encrypt → decrypt | 解密后与原始明文一致 |
| `test_verify_signature_valid` | 正确的 token/timestamp/nonce/encrypt | 验签通过 |
| `test_verify_signature_tampered` | 被篡改的 encrypt 或 token | 验签失败 |
| `test_extract_encrypt_xml` | XML body（`<Encrypt>...</Encrypt>`） | 提取出密文字符串 |
| `test_extract_encrypt_json` | JSON body（`{"Encrypt": "..."}`） | 提取出密文字符串 |
| `test_extract_encrypt_missing` | body 中无 Encrypt 字段 | 抛出异常 |
| `test_decrypt_corrupt_ciphertext` | 随机字符串 | 解密失败（不 crash） |
| `test_decrypt_wrong_key` | 正确密文 + 错误的 AES key | 解密失败 |
| `test_encoding_aes_key_padding` | 43 字符 Base64 key + "=" → 32 bytes | 正确解码 |

**Mock 策略**：纯函数，无需 mock。

#### 7.3.2 `test_token.py` — Token 管理

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_first_fetch` | 首次 `get_token()` | 发起 HTTP GET，返回 token |
| `test_cache_hit` | 两次连续 `get_token()` | 只发起一次 HTTP 请求 |
| `test_refresh_before_expiry` | token 距过期 < 5min | 自动刷新 |
| `test_concurrent_get_token` | 多个协程同时 `get_token()` | Lock 保护，只一次 HTTP 请求 |
| `test_fetch_api_error` | gettoken 返回 `errcode != 0` | 抛出 `TokenError` |
| `test_background_refresh_retry` | 后台刷新首次网络失败 | 指数退避重试，最多 3 次 |
| `test_background_refresh_gives_up` | 重试 3 次均失败 | `log.error`，保留旧 token |
| `test_refresh_task_cancel_previous` | 连续两次触发刷新 | 前一个 refresh task 被 cancel |

**Mock 策略**：用 `respx` mock `qyapi.weixin.qq.com/cgi-bin/gettoken`，注入预设 JSON 响应。

#### 7.3.3 `test_dedup.py` — 消息去重

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_new_message_not_seen` | 新 msg_id | `has_seen()` → False |
| `test_mark_then_check` | mark_seen → has_seen | True |
| `test_ttl_expiration` | mark_seen，等 TTL+1s 后 has_seen | False（已过期清理） |
| `test_lru_eviction` | 插入 max_size+1 个条目 | 最老的 20% 被淘汰 |
| `test_expired_entries_cleaned_on_check` | 多条过期 + 1 条有效 | 过期条目被清理，有效条目保留 |

**Mock 策略**：纯函数，控制 `time.time()` 或使用 `freezegun` / 手动注入时间戳。

#### 7.3.4 `test_sync.py` — 消息同步

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_first_drain_returns_empty` | `_drained=False`，sync_msg 返回消息 | 返回空列表，cursor 更新 |
| `test_normal_fetch_after_drain` | `_drained=True`，sync_msg 返回消息 | 返回 msg_list |
| `test_has_more_pagination` | sync_msg 返回 `has_more=1` | 循环拉取直到 `has_more=0` |
| `test_api_error_returns_empty` | sync_msg 返回 `errcode != 0` | 返回空列表，不崩溃 |
| `test_cursor_persist_and_load` | fetch → save → 新实例 load | cursor 恢复一致 |
| `test_cursor_atomic_write` | 写入过程中崩溃（部分写入） | 旧 cursor 文件不被破坏 |
| `test_empty_cursor_on_start` | 无持久化文件，首次启动 | cursor 为空字符串 |

**Mock 策略**：用 `respx` mock `qyapi.weixin.qq.com/cgi-bin/kf/sync_msg`；cursor 文件用 `tmp_path`。

#### 7.3.5 `test_sender.py` — 消息发送

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_send_text_correct_api_call` | touser + open_kfid + content | POST body 字段正确 |
| `test_send_text_with_custom_msgid` | 指定 msgid | POST body 中 msgid 为指定值 |
| `test_send_text_auto_msgid` | 不传 msgid | POST body 中 msgid 为 UUID |
| `test_send_text_api_error` | send_msg 返回 errcode != 0 | 不抛异常（记录日志） |

**Mock 策略**：用 `respx` mock `qyapi.weixin.qq.com/cgi-bin/kf/send_msg`，断言 POST body。

#### 7.3.6 `test_types.py` — 配置与类型

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_encoding_aes_key_length_validation` | 42 字符 key | `ValidationError` |
| `test_encoding_aes_key_exact_43` | 43 字符 key | 通过验证 |
| `test_port_range_validation` | port=0 或 port=65536 | `ValidationError` |
| `test_sync_interval_range` | sync_interval=0.5 或 61 | `ValidationError` |
| `test_to_agent_config_overlay_fields` | base AgentConfig + WechatKFConfig | model/max_turns/permission_mode/tools 被 overlay |
| `test_to_agent_config_keep_other_fields` | base 有 compression/sampling 等字段 | merge 后保持不变 |
| `test_to_agent_config_api_keys_merge` | base 有 deepseek key，wechat 有 anthropic key | 两者都保留 |
| `test_to_agent_config_base_urls_override` | base 和 wechat 都有同 provider URL | wechat 覆盖 base |
| `test_to_agent_config_none_base` | base=None | 从 AgentConfig 默认值构造 |

**Mock 策略**：纯 Pydantic 模型，无需 mock。

#### 7.3.7 `test_text_splitter.py` — 文本拆分

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_short_text_passthrough` | "你好" (len < 2000) | 原样返回单元素列表 |
| `test_exact_boundary` | 2000 字符文本 | 单元素列表 |
| `test_long_text_split` | 3500 字符 | 拆为 2 段，每段 ≤ 2000 |
| `test_split_at_newline` | 长文本中含 `\n` | 优先在换行处拆分 |
| `test_split_at_period` | 长文本中含 `。` | 优先在句号处拆分 |
| `test_split_at_space` | 无换行/句号，有空格 | 在空格处拆分 |
| `test_split_fallback_hard_cut` | 2000 字符内无任何自然断点 | 在 max_chars 处硬截断 |
| `test_empty_string` | "" | 返回空列表或 `[""]` |
| `test_multi_chunk` | 10000 字符 | 拆为 5 段，每段 ≤ 2000 |
| `test_custom_max_chars` | max_chars=500 | 按 500 拆分 |

**Mock 策略**：纯函数，无需 mock。

#### 7.3.8 `test_agent_bridge.py` — Agent 桥接

| 用例 | 输入 | 预期 |
|------|------|------|
| `test_session_creation` | 新 user_id + open_kfid | 创建新 SessionState |
| `test_session_reuse` | 同一 user_id 两次 process | 使用同一 SessionState |
| `test_session_timeout_lazy_cleanup` | session 超时后再次 process | 旧 session 被移除，创建新 session |
| `test_session_id_format` | touser="wm_xxx", open_kfid="kf_yyy" | session_id = "kf_yyy:wm_xxx" |
| `test_process_creates_and_closes_agent` | 正常消息 | Agent 被创建、stream 被消费、aclose 被调用 |
| `test_process_collects_text` | Agent 返回 "你好" + "世界" | 合并为 "你好世界" |
| `test_process_empty_reply_no_send` | Agent 无 assistant_text | 返回空列表（不推送空消息） |
| `test_process_error_with_partial_text` | 文本 + error 事件 | 返回已收集的文本 |
| `test_process_error_no_text` | 仅有 error 事件，无文本 | 返回通用错误提示 |
| `test_build_prompt_text` | msg_type="text", content="你好" | 直接透传 "你好" |
| `test_build_prompt_voice` | msg_type="voice", content="转文字结果" | "[语音消息] 转文字结果" |
| `test_build_prompt_image` | msg_type="image", content="" | "[图片消息]" |
| `test_clear_session` | clear_session 后 process | 创建新 session（旧 session 已被移除） |
| `test_per_session_lock_serialization` | 同用户并发 3 条消息 | 串行处理（Lock 保证） |
| `test_different_users_independent` | 不同用户并发 | 不同 Lock，可并行 |
| `test_agent_aclose_called_on_exception` | Agent.stream() 中途抛异常 | finally 中 aclose() 被调用 |

**Mock 策略**：

- Agent 通过 `patch("agent_core.agent.Agent")` mock，注入一个返回预设 `AgentMessage` 序列的 `stream()` 生成器
- 验证点包括：Agent 构造参数中的 `session=` 是否正确传入、`stream()` 的 `system=` 是否正确传入、`aclose()` 是否被调用
- 并发测试使用 `asyncio.gather()` 同时发起多个 `process()` 调用

### 7.4 集成测试用例

#### 7.4.1 `test_channel_pipeline.py` — 完整流水线

**测试目标**：验证从 HTTP 回调到消息发送的完整数据流。

```
HTTP POST /wechat (加密回调)
  → 验签 + 解密
  → 提取 callback_token
  → sync_msg API 拉取消息
  → Agent 推理（MockClient）
  → send_msg API 推送回复
```

| 用例 | 场景 | 验证点 |
|------|------|--------|
| `test_full_pipeline_text_message` | 用户发送文本 "你好" | ① HTTP 200 + body="success" ② send_msg 被调用且 content 为 Agent 返回的文本 |
| `test_full_pipeline_multi_turn` | 同一用户连续两次消息 | ① 两次 send_msg 均被调用 ② 第二次 Agent 收到包含第一次对话历史的 messages |
| `test_callback_response_within_timeout` | 正常回调 | HTTP 响应时间 < 100ms（回调在 create_task 后立即返回） |
| `test_ignore_non_kf_events` | suite_ticket 回调（MsgType != "kf_msg_or_event"） | 返回 "success"，不触发 sync_msg/Agent/send_msg |
| `test_ignore_self_messages` | sync_msg 返回 origin=4（客服发的） | 被过滤，不触发 Agent |

**Mock 策略**：

- HTTP 层用 `httpx.AsyncClient`（或 `starlette` 的 `TestClient`）发请求到 channel 的 starlette app
- sync_msg 和 send_msg 的 HTTP 调用用 `respx` mock
- Agent 层用 `MockClient`（复用 `tests/integration/mock_model.py`），通过 `patch("agent_core.query.loop.get_client", ...)` 注入

#### 7.4.2 `test_channel_lifecycle.py` — 生命周期

| 用例 | 场景 | 验证点 |
|------|------|--------|
| `test_start_loads_cursor` | `start()` 调用 | `MessageSyncer.load_cursor()` 被调用 |
| `test_stop_waits_pending_tasks` | 有 in-flight _process task 时 stop | ① 等待 task 完成（最多 30s）② task 完成后正常退出 |
| `test_stop_cancels_stuck_tasks` | in-flight task 卡住超过 30s | 超时后 cancel 剩余 task，不永久挂起 |
| `test_stop_closes_http_client` | stop() | `httpx.AsyncClient.aclose()` 被调用 |
| `test_polling_loop_fires` | 启动后等待 sync_interval | polling loop 周期性调用 sync_msg |
| `test_polling_loop_stops_on_shutdown` | `_shutdown_event.set()` | polling loop 退出 |
| `test_polling_loop_survives_error` | 单次 sync_msg 网络错误 | 记录 warning，下一轮继续（不中断 loop） |

**Mock 策略**：

- `start()` 中的 `uvicorn.Server.serve()` 需要 mock（避免实际监听端口），或直接测试 `_process()` / `_polling_loop()` 等内部方法
- sync_msg/send_msg 用 `respx` mock
- 用 `asyncio.Event` 控制时序

#### 7.4.3 `test_error_scenarios.py` — 错误场景

| 用例 | 场景 | 验证点 |
|------|------|--------|
| `test_sync_msg_errcode_handling` | sync_msg 返回 `errcode=40001`（无效 token） | 返回空列表，记录日志，不崩溃 |
| `test_agent_error_sends_fallback` | Agent.stream() 抛出异常 | 发送 "处理出错，请稍后再试。" |
| `test_agent_error_with_partial_reply` | Agent 先返回文本再抛异常 | 发送已收集的文本（部分回复优于错误提示） |
| `test_send_msg_failure_logged` | send_msg 网络错误 | 记录日志，不崩溃，不影响后续消息处理 |
| `test_decrypt_failure_returns_success` | 回调 body 密文损坏 | 返回 "success"（避免企业微信重试） |
| `test_missing_msg_signature` | 回调 URL 无 msg_signature 参数 | 返回 "success" |
| `test_signature_mismatch` | 签名不匹配 | 返回 403 |

**Mock 策略**：`respx` 控制 sync_msg/send_msg 的响应；mock Agent 控制 stream 行为；构造畸形回调 body。

#### 7.4.4 `test_concurrency.py` — 并发安全

| 用例 | 场景 | 验证点 |
|------|------|--------|
| `test_same_user_serialized` | 同用户并发 2 条消息 | 两条消息按顺序处理（Lock），第二条的 Agent 能看见第一条的回复在 SessionState 中 |
| `test_different_users_parallel` | 2 个不同用户各发 1 条消息 | 两个 Agent 并行执行，互不阻塞 |
| `test_lock_released_on_exception` | 同用户第一条消息 Agent 崩溃 | Lock 被释放，第二条消息能正常处理 |
| `test_cursor_no_race_condition` | 并发 sync_msg 调用 | cursor 更新无竞态（`asyncio.Lock` 或单 task 设计保证） |

**Mock 策略**：

- 用 `asyncio.Event` 控制 Agent 执行时序：在 mock Agent.stream() 中 await event.wait()，精确控制两个 task 的执行交错
- 验证 SessionState.messages 的最终内容是否正确（无消息丢失/覆盖）

### 7.5 测试执行与 CI

```bash
# 运行微信客服相关的所有测试
pytest tests/unit/wechat_kf/ tests/integration/wechat_kf/ -v

# 仅单元测试（无需网络，纯逻辑）
pytest tests/unit/wechat_kf/ -v

# 集成测试（需要 respx mock，无真实网络）
pytest tests/integration/wechat_kf/ -v

# 带覆盖率
pytest tests/unit/wechat_kf/ tests/integration/wechat_kf/ \
  --cov=agent_core.channels.wechat_kf \
  --cov=agent_core.bridge \
  --cov-report=term-missing
```

所有测试均为**离线测试**——单元测试检验纯逻辑，集成测试通过 `MockClient` + `respx` 模拟所有外部依赖（企业微信 API + LLM），无需真实 API key 或网络连接。CI 中直接作为 `pytest` 的标准 suite 运行。

### 7.6 测试用例统计

| 模块 | 单元测试 | 集成测试 | 合计 |
|------|---------|---------|------|
| crypto | 9 | — | 9 |
| token | 7 | — | 7 |
| dedup | 5 | — | 5 |
| sync | 6 | — | 6 |
| sender | 4 | — | 4 |
| types | 8 | — | 8 |
| text_splitter | 8 | — | 8 |
| agent_bridge | 17 | — | 17 |
| channel pipeline | — | 5 | 5 |
| channel lifecycle | — | 6 | 6 |
| error scenarios | — | 7 | 7 |
| concurrency | — | 4 | 4 |
| **合计** | **64** | **22** | **86** |

总计约 **86 个测试用例**，覆盖所有 15 个新文件的正常路径、错误路径和边界条件。