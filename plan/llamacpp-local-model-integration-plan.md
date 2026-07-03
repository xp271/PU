# llama.cpp 本地模型集成计划书

## 背景

当前 `agent_core` 支持 Anthropic、OpenAI、DeepSeek 三个远程 API 提供商，但不支持本地部署的 LLM。项目目录 `llama.cpp/` 下已有完整的 llama.cpp 二进制分发版（含 `llama-server.exe` 和 GGUF 模型文件 `Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf`），可以启动 OpenAI 兼容的 HTTP 服务。

**核心发现**：`llama-server.exe` 提供完整 OpenAI 兼容 API（`/v1/chat/completions`、`/v1/models` 等），支持 function calling / tools。无需额外代理层，直接在 `agent_core` 改 `base_url` 就能对接。

**目标**：新增 `ModelProvider.LLAMACPP`，让用户只需启动本地 `llama-server.exe`，然后在配置文件中切换 provider 即可使用本地模型。

---

## 实现方案

### 思路

参照现有 `DeepSeekClient` / `OpenAIClient` 模式：

- 复用 `openai.AsyncOpenAI` SDK
- 默认 `base_url` = `http://127.0.0.1:8080/v1`
- 注册为 `ModelProvider.LLAMACPP` 的客户端
- 无需 API key（llama-server 默认无认证），传占位值即可
- 连接失败 → retryable error（服务未启动的正常情况）

### 继承关系

`OpenAIClient` 和 `DeepSeekClient` 目前包含大量重复代码（convert_messages / convert_tools / _do_stream / _blocks_to_text 几乎逐字相同）。`LlamaCppClient` 直接继承 `OpenAIClient`，只需覆写差异部分（provider 声明、`__init__` 默认值、`classify_error`）。未来可考虑 `DeepSeekClient` 也继承 `OpenAIClient` 来消除冗余。

---

## 需要修改/新建的文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `src/agent_core/models.py` | 添加 `LLAMACPP = "llamacpp"` 枚举值和 API key 环境变量映射 |
| 修改 | `src/agent_core/llm/openai.py` | `StreamStart` 中 `provider` 由硬编码 `"openai"` 改为 `self.provider.value`，便于子类继承 |
| 修改 | `src/agent_core/llm/deepseek.py` | `StreamStart` 中 `provider` 由硬编码 `"deepseek"` 改为 `self.provider.value`（一致性） |
| 新建 | `src/agent_core/llm/llamacpp.py` | `LlamaCppClient` 实现，继承 `OpenAIClient` |
| 修改 | `src/agent_core/llm/__init__.py` | 添加 `from . import llamacpp  # noqa: F401` |
| 修改 | `src/agent_core/cli.py` | `_ENV_VAR_MAP` 添加 `LLAMACPP_API_KEY`；credential guard 允许无 API key 的 provider |
| 新建 | `scripts/start-llama-server.ps1` | 一键启动脚本（可选交付物） |
| 新建 | `tests/unit/test_llamacpp.py` | 单元测试 |

---

### 1. 新增 `ModelProvider.LLAMACPP`

**文件**: `src/agent_core/models.py`

```python
class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    LLAMACPP = "llamacpp"  # 新增
```

`known_models()` 中 llama.cpp 模型与 `allow_unknown_model=True` 配合使用：

```python
cls.LLAMACPP: ["*"],
```

> **注意**：`known_models` 中 `"*"` 是普通字面量，本身没有通配符语义。实际放行机制依赖 `AgentConfig.allow_unknown_model=True`——用户必须在配置文件中显式启用此开关。`"*"` 的作用是让 `ModelId.known_values()` 生成 `"llamacpp/*"` 条目，避免 validator 在已知值列表中找不到匹配时报错。如果不用 `"*"`，改为 `[]` 也是可以的，两种方式都需要 `allow_unknown_model=True` 才能真正放行任意模型名。

`get_api_key()` 的 `env_var_map` 新增：

```python
ModelProvider.LLAMACPP: "LLAMACPP_API_KEY",
```

> **重要**：此处和 `cli.py` 的 `_ENV_VAR_MAP` 必须同步添加 `"llamacpp": "LLAMACPP_API_KEY"`，否则 CLI 加载配置时无法从环境变量回退读取 API key。

### 2. 改造 OpenAIClient 和 DeepSeekClient 的 `StreamStart.provider`

`LlamaCppClient` 继承 `OpenAIClient`，会复用父类的 `stream_messages`。但当前 `OpenAIClient` 中 `StreamStart` 的 `provider` 字段是硬编码的 `"openai"`，`DeepSeekClient` 同样是硬编码的 `"deepseek"`。需要改为从 `ClassVar` 读取，这样子类可自动获得正确的 provider 标识。

**修改 `openai.py`**，将 `stream_messages` 中：

```python
yield StreamStart(
    turn_index=turn_index,
    provider="openai",
    model=self.model,
)
```

改为：

```python
yield StreamStart(
    turn_index=turn_index,
    provider=self.provider.value,
    model=self.model,
)
```

**修改 `deepseek.py`**，同理将 `provider="deepseek"` 改为 `provider=self.provider.value`。

### 3. LlamaCppClient 实现

**新建文件**: `src/agent_core/llm/llamacpp.py`

> **注意**：`@register_client` 装饰器**不接收参数**，provider 通过类的 `ClassVar` 属性声明。计划书此前写 `@register_client(ModelProvider.LLAMACPP)` 是错误的。

```python
from openai import APIConnectionError, AsyncOpenAI
from ..models import ModelProvider
from .base import BaseClient, register_client
from .errors import ErrorCategory
from .openai import OpenAIClient

_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"

@register_client
class LlamaCppClient(OpenAIClient):
    """Streaming client backed by a local llama.cpp server (OpenAI-compatible API)."""

    provider: ClassVar[ModelProvider] = ModelProvider.LLAMACPP

    def __init__(self, model, api_key, base_url):
        super().__init__(
            model=model,
            api_key=api_key or "not-needed",
            base_url=base_url or _DEFAULT_BASE_URL,
        )

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """连接拒绝/超时 → retryable，其余委托给 OpenAI 分类逻辑。"""
        if isinstance(exception, APIConnectionError):
            return "transient"
        return super().classify_error(exception)
```

关键点：
- 直接继承 `OpenAIClient`，message/tool 转换、`_do_stream`、`stream_messages` 全部复用
- 由于第 2 步已将 `StreamStart.provider` 改为 `self.provider.value`，`stream_messages` 会自动输出 `provider="llamacpp"`
- 默认 `api_key` 为 `"not-needed"`（llama-server 默认无需认证）
- 默认 `base_url` 指向本地 8080 端口
- 错误分类中 `APIConnectionError` 视为 retryable（覆盖父类的 `classify_error`）

### 4. 注册客户端

**文件**: `src/agent_core/llm/__init__.py`

```python
from . import anthropic  # noqa: F401
from . import deepseek   # noqa: F401
from . import openai     # noqa: F401
from . import llamacpp   # noqa: F401  # 新增
```

### 5. CLI 适配：credential guard 和 `_ENV_VAR_MAP`

**文件**: `src/agent_core/cli.py`

**5a.** `_ENV_VAR_MAP` 新增 llamacpp 条目（**必须**与 `models.py` 中 `get_api_key` 的 `env_var_map` 同步）：

```python
_ENV_VAR_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "llamacpp": "LLAMACPP_API_KEY",  # 新增
}
```

**5b.** credential guard（`_load_config` 中 `~cli.py:182-196`）需要放过无需认证的 provider。llama-server 默认无认证，用户不提供 API key 是合法场景。修改 guard 逻辑，在检查 API key 存在性时将 llamacpp 排除在外：

```python
# 无需 API key 的 provider
_NO_AUTH_PROVIDERS = {"llamacpp"}

# ... 在 credential guard 处:
if not any(api_keys.values()):
    provider = ModelId.parse(model).provider.value
    if provider not in _NO_AUTH_PROVIDERS:
        # 原有报错退出逻辑
        ...
```

### 6. 配置文件示例

用户配置 `agent-core.config.json`：

```json
{
    "model": "llamacpp/Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M",
    "api_keys": {
        "llamacpp": "not-needed"
    },
    "base_urls": {
        "llamacpp": "http://127.0.0.1:8080/v1"
    },
    "allow_unknown_model": true,
    "max_turns": 20,
    "permission_mode": "ask"
}
```

> `allow_unknown_model: true` 是必须的——llama.cpp 模型名不在 `known_models` 的硬编码列表中。

### 7. 启动脚本（可选）

**新建文件**: `scripts/start-llama-server.ps1`

推荐使用**多模型模式**（router mode），一次配置，后续只改 config 切换模型，无需重启 server：

```powershell
param(
    [string]$ModelsDir = ".\llama.cpp\models",
    [int]$Port = 8080,
    [int]$ContextSize = 8192,
    [int]$MaxModels = 2
)

.\llama.cpp\llama-server.exe `
    --models-dir $ModelsDir `
    --models-autoload `
    --models-max $MaxModels `
    --host 127.0.0.1 --port $Port `
    --tools `
    -c $ContextSize
```

关键参数：
- `--models-dir`：扫描目录下所有 `.gguf` 文件，自动注册为可用模型
- `--models-autoload`：API 请求时按需加载模型，闲置后自动卸载
- `--models-max`：内存中同时驻留的最大模型数（设为 2 可同时对比两个模型）
- `--tools`：启用 function calling，agent 调用工具的必要条件

启动后 `GET /v1/models` 会列出所有扫描到的模型名。

---

## 如何更换本地模型

### 方式一：多模型模式（推荐，无需重启 server）

`llama-server` 支持 router 模式，扫描整个模型目录，通过 API 动态切换：

**1. 将新模型放入 models 目录：**
```
llama.cpp/
└── models/
    ├── Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf   # 已有
    └── qwen2.5-7b-instruct-q4_k_m.gguf                 # 新下载的模型
```

**2. 使用 `--models-dir` 模式启动 server**（见上文启动脚本）。

**3. 确认模型被发现：**
```bash
curl http://127.0.0.1:8080/v1/models
# 返回的 model id 为 gguf 文件名（不含扩展名）
```

**4. 修改 `agent-core.config.json` 的 `model` 字段即可切换：**
```json
{
    "model": "llamacpp/qwen2.5-7b-instruct-q4_k_m"
}
```

无需重启 server，`--models-autoload` 会自动加载新模型并卸载旧模型。

### 方式二：单模型模式（简单直接）

每次换模型需要重启 server：

**1. 停止当前 server（Ctrl+C）**

**2. 重新启动，指定新模型路径：**
```powershell
.\llama.cpp\llama-server.exe -m .\llama.cpp\models\新模型.gguf --host 127.0.0.1 --port 8080 --tools -c 8192
```

**3. 修改 `agent-core.config.json` 的 `model` 字段**

---

## 验证步骤

1. 启动 `llama-server.exe`（执行 `scripts/start-llama-server.ps1`）
2. 健康检查：`curl http://127.0.0.1:8080/v1/models`
3. 修改 `agent-core.config.json`，将 `model` 改为 `llamacpp/模型名`
4. 运行 agent CLI，发送简单 prompt，确认流式响应正常
5. 发送需要工具调用的 prompt，确认 function calling 正常
6. 运行 `pytest tests/unit/test_llamacpp.py -v`

---

## 风险与注意事项

- **性能**：本地模型的推理速度取决于 GPU/CPU 算力，可能与云端 API 有较大差距
- **上下文长度**：需根据本地 GPU 显存合理设置 `-c` 参数
- **模型兼容性**：不同 GGUF 模型的 chat template 可能不同，llama-server 内置 Jinja 模板引擎可自动适配主流模型
- **工具调用**：本地模型对 function calling 的支持质量可能不如 GPT-4/Claude，建议使用支持工具调用的模型
- **代码重复**：当前 `openai.py` 与 `deepseek.py` 存在大量重复代码（convert_messages / convert_tools / _do_stream / _blocks_to_text），`LlamaCppClient` 继承 `OpenAIClient` 避免了新增第三份副本。后续可考虑让 `DeepSeekClient` 也继承 `OpenAIClient` 来消除冗余
