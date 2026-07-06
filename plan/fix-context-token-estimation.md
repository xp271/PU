# 修复：上下文 token 估算偏差导致压缩/截断未触发

## 问题

SWE-Bench 日志显示 agent 在 turn 32 时因超出 context 限制直接报错退出：

```
n_prompt_tokens: 51484, n_ctx: 50176
→ "request (51484 tokens) exceeds the available context size (50176 tokens)"
```

但自动压缩/截断机制**从未触发**（日志中无任何 `CompactMessage` 事件）。

## 根因

两个问题叠加：

### 1. `estimate_tokens_from_messages()` 严重低估真实 token 数

当前使用 `chars/4` 启发式估算 token 数（`budget.py:110`）。对代码密集内容偏差极大：
- 真实 API token 数：45000+
- 启发式估算：约 5000（差距 9x）

| 估算来源 | 值 | 说明 |
|---------|-----|------|
| `estimate_tokens_from_messages()` (chars/4) | ~5000 | 启发式，低估 9x |
| API 实际返回 `input_tokens` | 45166 (turn 31) | 真实值 |
| `truncation_threshold` | 6553 | 8192 × 0.8 |

低估的主要原因：
- 代码 / 日志输出等非自然语言 token 化效率差（1 token ≈ 2-3 chars 而非 4 chars）
- System prompt、工具 schema 等固定开销未被计入
- 大型 tool_result（50KB+）会被截断为 `_RESULT_PREVIEW_CHARS = 1500` 字符的预览，但这是次要因素

### 2. 截断判断链路

```python
# loop.py:336-337
estimated = estimate_tokens_from_messages(messages, system)  # → ~5000
if budget.needs_truncation(estimated):                        # ~5000 < 6553 → False → 跳过
    ...
```

`needs_truncation(5000)` 返回 `False`，因为 5000 < 6553。实际上消息已 45000+ token，远超阈值。

---

## 修复方案

### 方案选择

| 方案 | 复杂度 | 准确性 | 风险 |
|------|--------|--------|------|
| A. 调大 `chars/4` 分母 | 低 | 仍不可靠 | 过度截断英文文本 |
| B. 引入校准因子（实际/估算比） | 中 | 一般 | 比例不稳定 |
| **C. 用 API 实际返回的 `input_tokens` 做判断** | **低** | **准确** | 第一轮无实际值需 fallback |

**选方案 C**。

### 实现细节

#### 文件 1：`src/agent_core/query/budget.py`

##### 变更 1.1：`TokenBudget` 新增字段

```python
@dataclass
class TokenBudget:
    # ... 现有字段 ...

    # 新增：上一轮 API 返回的实际 input_tokens（ground truth）
    last_actual_input_tokens: int = 0
```

##### 变更 1.2：`record_turn()` 存储实际值

在现有方法末尾增加一行：

```python
def record_turn(
    self, turn_index: int, input_tokens: int, output_tokens: int
) -> None:
    # ... 现有逻辑 ...
    self.last_actual_input_tokens = input_tokens  # 新增
```

##### 变更 1.3：`needs_truncation()` 使用 max(实际, 估算)

```python
def needs_truncation(self, estimated_tokens: int) -> bool:
    """截断阈值检查。优先使用上一轮 API 返回的实际 input_tokens，
    因为它精确反映真实使用量；启发式估算作为兜底（第一轮或实际值不可用时）。"""
    effective = max(estimated_tokens, self.last_actual_input_tokens)
    return effective > self.truncation_threshold
```

##### 变更 1.4：新增 `reset_last_actual()` 方法

压缩或截断成功后，消息列表已被裁剪，旧的 `last_actual_input_tokens` 不再有效。必须重置，否则下一轮检查时会误判仍需截断，形成无限压缩循环。

```python
def reset_last_actual(self) -> None:
    """压缩/截断后重置，避免使用已失效的旧值误判。"""
    self.last_actual_input_tokens = 0
```

##### 变更 1.5：提高 llamacpp provider default + 截断比例

```python
_PROVIDER_CONTEXT_DEFAULTS: dict[ModelProvider, int] = {
    ModelProvider.LLAMACPP: 32768,  # 从 8192 提高到 32768
}
_TRUNCATION_RATIO = 0.85  # 从 0.8 提高到 0.85
```

#### 文件 2：`src/agent_core/query/loop.py`

##### 变更 2.1：压缩成功后重置 `last_actual_input_tokens`

在 `loop.py` 压缩成功路径中（约第 382 行 `estimated = new_estimated` 之后），加入 reset 调用：

```python
estimated = new_estimated
budget.reset_last_actual()  # 新增：消息已压缩，旧 token 计数失效
```

##### 变更 2.2：压缩后仍超阈值 → 硬截断后重置

在 `loop.py` 第 386-390 行：

```python
if budget.needs_truncation(estimated):
    messages = hard_truncate(
        messages, max_tokens=budget.truncation_threshold
    )
    tool_context.messages = messages
    budget.reset_last_actual()  # 新增
```

##### 变更 2.3：压缩失败 → 硬截断后重置

在 `loop.py` 第 398-406 行的 except 分支：

```python
messages = hard_truncate(
    messages, max_tokens=budget.truncation_threshold
)
tool_context.messages = messages
budget.reset_last_actual()  # 新增
```

##### 变更 2.4：直接截断后重置

在 `loop.py` 第 409-413 行（compression unavailable 分支）：

```python
messages = hard_truncate(
    messages, max_tokens=budget.truncation_threshold
)
tool_context.messages = messages
budget.reset_last_actual()  # 新增
```

#### 文件 3：`scripts/swe_bench/run_local.py`

##### 变更 3.1：调整默认 `--ctx-size`

CLI 参数不再硬编码 8192，改为更大的默认值，与 `budget.py` 中提高后的 llamacpp provider default 保持一致：

```python
g_model.add_argument(
    "--ctx-size", type=int, default=32768,
    help="上下文窗口大小（默认 32768）",
)
```

同时修改 `_start_llamacpp_server` 的默认值：

```python
async def _start_llamacpp_server(
    model_path: Path,
    *,
    ctx_size: int = 32768,  # 从 8192 改为 32768
    ...
```

注意：**不改为 `None`**。因为 `_start_llamacpp_server` 直接将 `str(ctx_size)` 传给 llama-server 命令行参数，`None` 会导致 `--ctx-size None` 启动失败。

### 效果

修改后，turn N+1 的截断判断流程变为：

```
last_actual_input_tokens = 44659     (turn 29 API 返回)
estimated = estimate_tokens_from_messages(...)  → ~5000
effective = max(5000, 44659) = 44659
44659 > 27853 (32768 × 0.85) → True → 触发压缩/截断 ✓
```

压缩成功后，`last_actual_input_tokens` 被重置为 0，下一轮重新用启发式估算 + 新 API 返回的实际值做判断，不会无限循环。

---

## 验证方式

1. 运行同一 instance，观察日志中是否出现 `CompactMessage`（compression triggered）
2. 确认不再出现 `exceed_context_size_error`
3. 单元测试：`tests/unit/test_budget.py` 添加以下测试用例：
   - `test_needs_truncation_uses_max_of_estimated_and_actual` — 验证 `max(est, actual)` 逻辑
   - `test_reset_last_actual` — 验证重置后 `last_actual_input_tokens == 0`
   - `test_needs_truncation_after_reset` — 验证重置后仅依赖估算值
   - `test_record_turn_stores_actual_input_tokens` — 验证 `record_turn` 存储实际值

---

## 影响范围

| 文件 | 修改 |
|------|------|
| `src/agent_core/query/budget.py` | 新字段 `last_actual_input_tokens` + `needs_truncation()` 逻辑 + `reset_last_actual()` + provider default + ratio |
| `src/agent_core/query/loop.py` | 4 处截断/压缩后调用 `budget.reset_last_actual()` |
| `scripts/swe_bench/run_local.py` | `--ctx-size` 默认值 8192 → 32768，`_start_llamacpp_server` 默认值同步修改 |
| `tests/unit/test_budget.py` | 新增 4 个测试用例 |
