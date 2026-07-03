# 统计 token 时分开统计输入和输出 token 的更改计划书

## 目标
将 token 统计从 `total_tokens` 改为分别统计输入 token 和输出 token，提升 token 预算的精确性。

## 现状分析
- `DoneMessage.total_tokens` 仍为 `int`，包含输入和输出 token 的总和
- `AgentResult.total_tokens` 仍为 `int`，包含输入和输出 token 的总和
- 压缩模块 `summarizer.py` 和 `consistency.py` 仍使用 `total_tokens`
- LLM 客户端中，OpenAI 和 DeepSeek 已返回 `input_tokens` 和 `output_tokens` 的分离统计，但 Anthropic 仍使用 `total_tokens`

## 变更内容

### 1. 修改 `src/agent_core/models.py`
- **位置**: 搜索 `total_tokens: int`
- **变更**: 将 `total_tokens` 替换为 `input_tokens: int` 和 `output_tokens: int`
- **涉及结构**:
  - `AgentResult`: 从单个 `total_tokens: int` 改为 `input_tokens: int` 和 `output_tokens: int`
  - `DoneMessage`: 从单个 `total_tokens: int` 改为 `input_tokens: int` 和 `output_tokens: int`

### 2. 修改 `src/agent_core/llm/events.py`
- **位置**: `TokenCounts` 类定义
- **变更**: 将 `total_tokens: int` 替换为 `input_tokens: int` 和 `output_tokens: int`
- **说明**: 该定义已存在分离统计的字段，只需确认模型层与事件层一致

### 3. 修改 `src/agent_core/llm/anthropic.py`
- **位置**: `_do_stream` 方法中解析 Anthropic 返回的 `usage` 字段
- **变更**: 将 `usage.total_tokens` 改为 `usage.input_tokens` 和 `usage.output_tokens`
- **说明**: Anthropic SDK 返回的 `usage` 包含 `input_tokens` 和 `output_tokens` 字段

### 4. 修改 `src/agent_core/compression/summarizer.py`
- **位置**: `CompressionConsistency` 类定义
- **变更**: 将 `tokens_consumed: int` 替换为 `input_tokens_consumed: int` 和 `output_tokens_consumed: int`
- **说明**: 压缩调用 LLM 时，需分别统计压缩请求的输入和输出 token

### 5. 修改 `src/agent_core/compression/consistency.py`
- **位置**: 搜索 `tokens_consumed` 相关逻辑
- **变更**: 将 `tokens_consumed: int` 替换为 `input_tokens_consumed: int` 和 `output_tokens_consumed: int`
- **说明**: 压缩验证模块需与 `CompressionConsistency` 保持一致

## 影响范围
- 模型定义层：`AgentResult` 和 `DoneMessage` 的 token 统计接口
- 事件定义层：`TokenCounts` 的 token 统计字段
- Anthropic LLM 客户端：`usage.total_tokens` 的解析逻辑
- 压缩模块：`CompressionConsistency` 和 `summarizer.py` 的 token 统计接口

## 实施步骤
1. 依次修改上述五个文件，确保字段名和类型完全一致
2. 更新相关测试用例（如有）
3. 运行项目测试套件以验证更改

## 备注
- 此更改为纯类型/字段名调整，不改变业务逻辑
- 建议先备份原文件再进行修改
