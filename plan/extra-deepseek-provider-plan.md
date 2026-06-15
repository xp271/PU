# Phase 3: DeepSeek Provider 适配计划

## Context

DeepSeek API (`api.deepseek.com`) 完全兼容 OpenAI Chat Completions 接口格式，可直接复用 `openai` Python SDK。当前项目已支持 Anthropic 和 OpenAI 两个 provider，新增 DeepSeek 遵循一样的注册模式。

## 关键发现

| 项目 | DeepSeek | OpenAI |
|------|----------|--------|
| Base URL | `https://api.deepseek.com` | `https://api.openai.com/v1` |
| SDK | `openai` (同一个包) | `openai` |
| Auth | `Bearer ${DEEPSEEK_API_KEY}` | `Bearer ${OPENAI_API_KEY}` |
| 模型 | `deepseek-v4-pro`, `deepseek-v4-flash` | `gpt-4o`, `gpt-4.1` |
| Streaming | 兼容 OpenAI 格式 | 原生 |
| Tool Calling | 兼容 OpenAI 格式 | 原生 |
| Token 用量 | `usage.prompt_tokens` / `completion_tokens` | 同 |
| 思考模式 | `extra_body={"thinking": {"type": "enabled"}}` | 无 |
| 特有错误码 | 402 (余额不足) | 无 |

## 涉及文件

| 文件 | 改动 |
|------|------|
| `src/agent_core/models.py` | ① `ModelProvider` 新增 `DEEPSEEK = "deepseek"`；② `known_models()` 新增 DeepSeek 模型列表；③ `get_api_key()` 新增 `DEEPSEEK_API_KEY` 环境变量映射 |
| `src/agent_core/llm/deepseek.py` | **新增** — `DeepSeekClient(BaseClient)`，参考 `OpenAIClient` 实现，差异：默认 `base_url="https://api.deepseek.com"`、402 错误映射为 `fatal`、`StreamStart.provider="deepseek"` |
| `src/agent_core/llm/__init__.py` | 新增 `from . import deepseek` 触发 `@register_client` |
| `src/agent_core/query/budget.py` | `_MODEL_CONTEXT_WINDOWS` 新增 DeepSeek 模型上下文窗口（deepseek-v4: 128K） |
| `tests/unit/` | 新增 `test_deepseek_provider.py` — 单元测试：convert_messages / convert_tools / normalize_stop_reason / classify_error |
| `tests/integration/` | 可选：`test_deepseek_live.py` — 需要真实 API key 的 E2E 冒烟测试 |

## 实现步骤

### Step 1: `models.py` — 注册 provider 元数据

```python
# ModelProvider enum 新增成员
DEEPSEEK = "deepseek"

# known_models() 新增
cls.DEEPSEEK: [
    "deepseek-v4-pro",
    "deepseek-v4-flash",
]

# AgentConfig.get_api_key() 的 env_var_map 新增
ModelProvider.DEEPSEEK: "DEEPSEEK_API_KEY",
```

### Step 2: `llm/deepseek.py` — 核心 provider 实现

以 `OpenAIClient` 为蓝本，创建 `DeepSeekClient`：

- `provider = ModelProvider.DEEPSEEK`
- `@register_client` 装饰
- `__init__`: 默认 `base_url="https://api.deepseek.com"`（当用户未指定时）
- `convert_messages`: 与 OpenAI 相同（API 格式兼容）
- `convert_tools`: 与 OpenAI 相同
- `normalize_stop_reason`: 与 OpenAI 相同（`stop`/`length`/`tool_calls`/`content_filter` 映射）
- `classify_error`: 与 OpenAI 基本相同，额外处理 402 → `fatal`（余额不足不可重试）
- `stream_messages` / `_do_stream`: 与 OpenAI 相同结构，调整 `provider` 字符串和日志

### Step 3: `llm/__init__.py` — 触发注册

```python
from . import deepseek  # noqa: F401
```

### Step 4: `query/budget.py` — 上下文窗口配置

```python
(ModelProvider.DEEPSEEK, "deepseek-v4-pro"): 128_000,
(ModelProvider.DEEPSEEK, "deepseek-v4-flash"): 128_000,
```

### Step 5: 测试

- 单元测试覆盖 4 个 classmethod（`convert_messages`, `convert_tools`, `normalize_stop_reason`, `classify_error`）
- 确保现有 189 个测试无回归

## 不实现的内容

- 思考模式 (`thinking: {type: "enabled"}`) 特殊处理 — 作为 `extra_body` 透传，暂不单独暴露
- `deepseek-chat` / `deepseek-reasoner` 旧模型名 — 文档注明 2026-07-24 弃用，不加入已知列表

## Verification

```bash
# 单元测试
python -m pytest tests/unit/test_deepseek_provider.py -v

# 确保无回归
python -m pytest tests/ -x -q
```
