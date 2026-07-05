# QQ 机器人：Shell 风格指令集计划书（v2）

> **目标**：用类 Unix shell 指令（`/cd`、`/ls`、`/clear`、`/help`）替代 `/workspace`，所有指令不走 LLM、即时生效、返回提示。

---

> **状态**：📋 计划阶段，尚未实现。
>
> **审查记录**：
> - v1（初稿）→ v2（2026-07-05：去掉 `/skills`；修正 `allow_shell_commands` 仅约束文件系统指令；修正 `workspace_root=None` 时 `/ls` 和 `/cd` 的 fallback；`_resolve_path` 不处理 `--reset`；`/help` 文本补充 `/ls` 参数说明）

---

## 0. 背景与动机

### 0.1 现状

QQBot 当前仅有 `/workspace` 一条 shell 指令（来自 `qqbot-dynamic-workspace-switch-plan.md` v3）：

```
/workspace              → 显示当前工作目录
/workspace <abs-path>   → 切换工作目录 + 清历史
/workspace --reset      → 重置为默认 + 清历史
```

### 0.2 痛点

1. **不支持相对路径** — 用户无法 `/workspace subdir`，只能敲绝对路径
2. **不支持 `..`** — 无法返回上级目录
3. **无 `/ls`** — 想看目录内容必须依赖 LLM 调用 Glob 工具
4. **CLI 已有 `/clear`、`/help` 但 QQBot 独缺** — 用户在 QQ 群聊里无法清历史或查看帮助
5. **命名不够直觉** — Linux 用户更习惯 `/cd` 而不是 `/workspace`

### 0.3 目标体验

```
用户: /ls
Bot:  当前工作目录: C:\PU (默认)
      📁 src/          📁 tests/         📁 plan/
      📄 README.md     📄 pyproject.toml

用户: /cd src
Bot:  工作目录已切换至: C:\PU\src
      对话历史已重置。

用户: /ls agent_core/
Bot:  C:\PU\src\agent_core\
      📁 bridge/       📁 channels/      📁 cli/
      📁 session/      📁 tools/         📄 agent.py
      ...

用户: /cd ..
Bot:  工作目录已切换至: C:\PU
      对话历史已重置。

用户: /cd /etc        （绝对路径，超出原始 workspace_root）
Bot:  无法切换到工作空间外的目录: C:\etc

用户: /clear
Bot:  对话历史已清除。

用户: /help
Bot:  QQBot 可用指令:
        /cd [路径]      切换工作目录（支持相对/绝对，.. 返回上级）
        /cd --reset     重置为默认工作目录
        /ls [路径]      列出目录内容（一级，可选子路径）
        /clear          清除对话历史
        /help           显示此帮助
```

---

## 1. CLI 指令 → QQBot 可移植性分析

| CLI 指令 | 功能 | QQBot 移植 | 理由 |
|---|---|---|---|
| `/model` | 切换模型 | ❌ | QQBot 配置固定，不可动态改 |
| `/key` | 设置 API key | ❌ | 安全风险，不应在聊天中暴露密钥 |
| `/clear` | 清除对话历史 | ✅ | 实用，等价于"开始新对话" |
| `/skills` | 列出已加载技能 | ❌ | QQBot 线程内 `os.getcwd()` 不可靠，且技能信息对 QQ 用户价值低 |
| `/sessions` | 列出持久化会话 | ❌ | CLI 专有，QQBot 无 Phase B 持久化 |
| `/logs` | 对话日志指标 | ❌ | CLI 专有，输出在 stdout |
| `/help` | 显示帮助 | ✅ | 必须，用户需要知道有哪些指令 |
| `/quit` | 退出 | ❌ | QQBot 不退 |

**QQBot 新增指令**（替代 `/workspace`）：

| 指令 | 功能 | 说明 |
|---|---|---|
| `/cd [path]` | 切换工作目录 | 支持相对/绝对路径，`..` 支持，边界约束 |
| `/cd --reset` | 重置为默认 | 相当于 `/workspace --reset` |
| `/ls [path]` | 列出目录 | 一级列表，区分文件/文件夹，支持可选子路径参数 |
| `/clear` | 清除对话历史 | 与 CLI `/clear` 行为一致 |
| `/help` | 显示帮助 | 硬编码文本，不走 LLM |

---

## 2. 核心设计

### 2.1 指令路由

`handle_message()` 中，所有 `/` 前缀的消息在进入 Agent 前被拦截：

```
handle_message(content):
  if content starts with "/cd":      → _handle_cd_command()
  if content starts with "/ls":      → _handle_ls_command()
  if content == "/clear":            → _handle_clear_command()
  if content == "/help":             → _handle_help_command()
  else if content starts with "/":   → "未知指令: /xxx。输入 /help 查看可用指令。"
  else:                              → Agent.process() [正常 LLM 流程]
```

> **注意**：`/workspace` 不再被识别，完全删除。用户打 `/workspace` 会落在 "未知指令" 分支，返回提示引导 `/help`。

### 2.2 路径解析辅助方法 `_resolve_path`

`/cd` 和 `/ls` 共享同一个路径解析逻辑，但不共享 `--reset`（那是 `/cd` 专属）：

```python
def _resolve_path(
    self, user_id: str, channel_id: str, arg: str,
) -> tuple[str | None, str | None]:
    """解析相对于当前 effective workspace 的路径。

    Returns:
        (resolved_path, error_msg)

        - error_msg 非 None → 路径不合法，resolved_path 为 None
        - arg 为空字符串 → 返回 (effective, None)，即"显示当前"
        - 绝不返回 "--reset"——reset 由 _handle_cd_command 自行处理
    """
    effective = (
        self._bridge.get_session(user_id, channel_id).workspace_override
        or self._agent_config.workspace_root
        or os.getcwd()
    )

    if not arg:
        return effective, None  # 调用方决定是"显示"还是"列出"

    if os.path.isabs(arg):
        target = os.path.abspath(arg)
    else:
        target = os.path.abspath(os.path.join(effective, arg))

    # 安全检查：不能超出原始 workspace_root
    original_root = self._agent_config.workspace_root
    if original_root is not None:
        root_norm = os.path.normpath(original_root)
        target_norm = os.path.normpath(target)
        if not (target_norm == root_norm
                or target_norm.startswith(root_norm + os.sep)):
            return None, f"无法切换到工作空间外的目录: {target}"

    if not os.path.isdir(target):
        return None, f"目录不存在: {target}"

    return target, None
```

**关键设计决策：**

- `effective` 计算时，如果 `workspace_override` 和 `workspace_root` 都为 `None`，回退到 `os.getcwd()`——这覆盖了 QQBot `workspace_root` 未配置时 `/ls` 仍能列出当前进程目录的场景。
- `_resolve_path` **不** 处理 `--reset`：`_handle_cd_command` 在自己的入口处 `if arg == "--reset"` 提前返回。
- `/ls` 也调 `_resolve_path`——若 `arg` 为空，返回 `(effective, None)`，调用方列出该目录；若有参数，解析后列出目标目录但不切换。

### 2.3 `/cd` 处理器

```python
async def _handle_cd_command(self, user_id, channel_id, content):
    """处理 /cd 指令。"""
    # --- gating ---
    if not self._config.allow_shell_commands:
        await self._send_message(user_id, channel_id,
                                 "Shell 指令功能未启用。")
        return

    session = self._bridge.get_session(user_id, channel_id)
    arg = content[len("/cd"):].strip()

    # --- 无参：显示当前 ---
    if not arg:
        effective = (session.workspace_override
                     or self._agent_config.workspace_root
                     or os.getcwd())
        suffix = ""
        if session.workspace_override is None and self._agent_config.workspace_root is None:
            suffix = " (默认)"
        await self._send_message(user_id, channel_id,
                                 f"当前工作目录: {effective}{suffix}")
        return

    # --- reset ---
    if arg == "--reset":
        session.workspace_override = None
        session.clear_history()
        default = self._agent_config.workspace_root or os.getcwd()
        await self._send_message(user_id, channel_id,
            f"工作目录已重置为默认: {default}\n对话历史已重置。")
        return

    # --- 切换 ---
    new_path, err = self._resolve_path(user_id, channel_id, arg)
    if err is not None:
        await self._send_message(user_id, channel_id, err)
        return

    session.workspace_override = new_path
    session.clear_history()
    await self._send_message(user_id, channel_id,
        f"工作目录已切换至: {new_path}\n对话历史已重置。")
```

### 2.4 `/ls` 处理器

```python
async def _handle_ls_command(self, user_id, channel_id, content):
    """处理 /ls 指令。"""
    # --- gating ---
    if not self._config.allow_shell_commands:
        await self._send_message(user_id, channel_id,
                                 "Shell 指令功能未启用。")
        return

    arg = content[len("/ls"):].strip()
    target, err = self._resolve_path(user_id, channel_id, arg)
    if err is not None:
        await self._send_message(user_id, channel_id, err)
        return

    # target is always non-None when err is None (arg="" → effective)
    entries = sorted(os.listdir(target), key=lambda n: (not os.path.isdir(
        os.path.join(target, n)), n.lower()))
    if not entries:
        await self._send_message(user_id, channel_id,
                                 f"{target}\n(空目录)")
        return

    # format into columns
    lines = [target]
    formatted: list[str] = []
    for e in entries:
        full = os.path.join(target, e)
        if os.path.isdir(full):
            formatted.append(f"📁 {e}/")
        else:
            formatted.append(f"📄 {e}")
    col_width = max(len(f) for f in formatted) + 2
    per_row = max(1, 60 // col_width)  # ~30 中文字宽 ≈ 60 半角
    for i in range(0, min(len(formatted), 50), per_row):
        row = "".join(f.ljust(col_width) for f in formatted[i:i + per_row])
        lines.append(row.rstrip())
    if len(entries) > 50:
        lines.append(f"... 及另外 {len(entries) - 50} 个条目")
    await self._send_message(user_id, channel_id, "\n".join(lines))
```

### 2.5 `/ls` 输出格式示例

```
C:\PU\
📁 src/          📁 tests/         📁 plan/
📄 README.md     📄 pyproject.toml
```

规则：
- 首行显示目标目录的绝对路径
- 文件夹用 `📁 name/`，文件用 `📄 name`
- 按字母排序，目录优先
- 多列排版，每列宽度取最长条目 + 2 空格 padding
- `per_row` 按 ~30 中文字宽（60 半角字符）计算
- **超长截断**：条目超过 50 个时只显示前 50 条 + `... 及另外 N 个条目`
- 空目录显示 `(空目录)`

### 2.6 `/clear` 处理器

```python
async def _handle_clear_command(self, user_id, channel_id):
    session = self._bridge.get_session(user_id, channel_id)
    session.clear_history()
    await self._send_message(user_id, channel_id, "对话历史已清除。")
```

**不受 `allow_shell_commands` 约束**——`/clear` 不涉及文件系统，始终可用。

### 2.7 `/help` 处理器

```python
_QQBOT_HELP = """\
QQBot 可用指令:
  /cd [路径]      切换工作目录（支持相对/绝对路径，.. 返回上级）
  /cd --reset     重置为默认工作目录
  /ls [路径]      列出目录内容（一级，可选子路径）
  /clear          清除对话历史
  /help           显示此帮助"""

async def _handle_help_command(self, user_id, channel_id):
    await self._send_message(user_id, channel_id, _QQBOT_HELP)
```

**不受 `allow_shell_commands` 约束**——始终可用。

### 2.8 指令汇总

| 指令 | 参数 | 行为 | 清历史 | 受 `allow_shell_commands` 约束 |
|---|---|---|---|---|
| `/cd` | 无 | 显示当前工作目录 | ❌ | ✅ |
| `/cd PATH` | 相对/绝对路径，`..` | 切换目录 | ✅ | ✅ |
| `/cd --reset` | — | 重置为默认 | ✅ | ✅ |
| `/ls` | 无 | 列出当前目录 | ❌ | ✅ |
| `/ls PATH` | 子路径 | 列出指定子目录 | ❌ | ✅ |
| `/clear` | 无 | 清除对话历史 | ✅ | ❌（始终可用） |
| `/help` | 无 | 显示帮助 | ❌ | ❌（始终可用） |

---

## 3. 改动范围

### 3.1 修改文件

| 文件 | 变更内容 | 影响 |
|---|---|---|
| `channels/qq_bot/channel.py` | ① 删除 `_handle_workspace_command`；② `handle_message` 重构为 `/` 通用路由；③ 新增 `_resolve_path`；④ 新增 `_handle_cd_command`、`_handle_ls_command`、`_handle_clear_command`、`_handle_help_command`；⑤ 新增 unknown command 处理 | 仅 QQBot |
| `channels/qq_bot/types.py` | `allow_workspace_switch` → `allow_shell_commands`（描述与字段名都改） | 仅 QQBot 配置 |
| `tests/unit/qq_bot/test_channel.py` | ① `TestWorkspaceCommand` → `TestCdCommand`（重写 + 新增边界）；② 新增 `TestLsCommand`、`TestClearCommand`、`TestHelpCommand`、`TestUnknownCommand` | 测试 |
| `plan/qqbot-dynamic-workspace-switch-plan.md` | 添加废弃标记，指向 v2 本计划书 | 文档 |

### 3.2 不改的文件

| 文件 | 原因 |
|---|---|
| `session/state.py` | `workspace_override` 字段不变 |
| `bridge/agent_bridge.py` | `get_session()` + `process()` overlay 逻辑不变 |
| `permissions/engine.py` | 安全引擎不受影响 |
| `tools/_io.py` | `check_workspace()` 不受影响 |
| `cli/_main.py` | CLI 独立运行 |

---

## 4. 安全分析

| 威胁 | 缓解 |
|---|---|
| `/cd ..` 越权爬出原始 `workspace_root` | `_resolve_path` 中 clamp：`target` 必须以 `original_root` 为前缀，否则拒绝 |
| `/cd /etc` 切换到系统敏感目录 | 同上 |
| `/ls ../../..` 遍历任意目录 | 复用 `_resolve_path`，被边界约束阻断 |
| `/clear` 清除不该清的数据 | 只清 `messages` + `_written_count`，权限记忆保留，与 CLI `/clear` 一致 |
| `workspace_root=None` 时切换无边界 | 既有行为（`check_workspace` 在 `None` 时跳过边界检查），非本次引入 |
| `/help` 信息泄露 | 硬编码文本，不含任何配置细节 |
| 并发投递竞态 | 依赖 botpy WebSocket 串行投递同一用户消息 + `AgentBridge` per-session Lock |

---

## 5. 边界情况

| 场景 | 行为 |
|---|---|
| `/cd`（无参） | 显示当前工作目录；若 workspace_root=None 且无 override，显示 `os.getcwd()` + `(默认)` 标记 |
| `/cd --reset` | 重置为 `config.workspace_root`（或 `os.getcwd()` fallback）+ 清历史 |
| `/cd subdir`（相对） | 基于当前 effective workspace 解析 |
| `/cd ..` | 上一级，被 clamp 在原始 workspace_root 内 |
| `/cd ../../..` | 多次 `..`，被 clamp 在原始 workspace_root 内 |
| `/cd /abs/path` | 绝对路径，若不在原始 workspace_root 内则拒绝 |
| `/cd not-exist` | 返回 "目录不存在" |
| `/ls`（无参） | 首行列当前 effective workspace 路径，然后列目录内容 |
| `/ls subdir` | 列出当前目录下的 subdir（不切换 effective） |
| `/ls not-exist` | 返回 "目录不存在" |
| `/ls ../../etc` | 被边界约束阻断 |
| `/clear` | 清空 messages + 重置 `_written_count` |
| `/help` | 返回硬编码帮助文本 |
| 未知 `/xxx` | "未知指令: /xxx。输入 /help 查看可用指令。" |
| `/cdxyz` 前缀匹配 | 会被路由到 `/cd` handler，arg=`"xyz"`→尝试切换目录 `xyz` → "目录不存在" |
| 非 `/` 前缀 | 正常走 Agent LLM 流程 |
| `allow_shell_commands=False` | 仅 `/cd` `/ls` 返回 "Shell 指令功能未启用。"；`/clear` `/help` 仍可用 |
| workspace_root=None 且无 override | `/cd` 显示 `os.getcwd() (默认)`；`/ls` 列出 `os.getcwd()`；`/cd path` 正常切换（无边界约束） |

---

## 6. 实现步骤

| # | 文件 | 变更 |
|---|---|---|
| 1 | `types.py` | `allow_workspace_switch` → `allow_shell_commands`（字段名+描述） |
| 2 | `channel.py` | 删除 `_handle_workspace_command`；`import os` 从函数体内提升至模块级 |
| 3 | `channel.py` | `handle_message` 重构为 `/` 通用路由 |
| 4 | `channel.py` | 新增 `_resolve_path` 辅助方法 |
| 5 | `channel.py` | 新增 `_handle_cd_command` |
| 6 | `channel.py` | 新增 `_handle_ls_command` |
| 7 | `channel.py` | 新增 `_handle_clear_command` |
| 8 | `channel.py` | 新增 `_handle_help_command` |
| 9 | `channel.py` | 新增 unknown command 处理 |
| 10 | 测试 | 重写 `TestWorkspaceCommand` → `TestCdCommand` + 边界测试（含 workspace_root=None 场景） |
| 11 | 测试 | 新增 `TestLsCommand`（含空目录、超长截断、workspace_root=None） |
| 12 | 测试 | 新增 `TestClearCommand`、`TestHelpCommand`、`TestUnknownCommand` |
| 13 | 测试 | 新增 `TestAllowShellCommandsFlag`（验证 `/clear` `/help` 不受限） |
| 14 | 测试 | 全量回归（176 → ~200+ 用例） |
| 15 | 文档 | 旧计划书添加废弃标记 |

> 预估 0.5 天。

---

## 7. 用户提示一览

| 触发 | 回复 |
|---|---|
| `/cd` | `当前工作目录: C:\PU` 或 `当前工作目录: C:\somewhere (默认)` |
| `/cd src` | `工作目录已切换至: C:\PU\src\n对话历史已重置。` |
| `/cd ..` | `工作目录已切换至: C:\PU\n对话历史已重置。` |
| `/cd /etc`（越权） | `无法切换到工作空间外的目录: C:\etc` |
| `/cd not-exist` | `目录不存在: C:\PU\not-exist` |
| `/cd --reset` | `工作目录已重置为默认: C:\PU\n对话历史已重置。` |
| `/ls` | `C:\PU\n📁 src/     📁 tests/    📁 plan/\n📄 README.md    📄 pyproject.toml` |
| `/ls src` | `C:\PU\src\n📁 agent_core/    📁 ...` |
| `/ls not-exist` | `目录不存在: C:\PU\not-exist` |
| `/ls ../../etc`（越权） | `无法切换到工作空间外的目录: C:\etc` |
| `/clear` | `对话历史已清除。` |
| `/help` | 多行帮助文本 |
| `/workspace`（废弃） | `未知指令: /workspace。输入 /help 查看可用指令。` |
| `allow_shell_commands=False` + `/cd` 或 `/ls` | `Shell 指令功能未启用。` |
| `allow_shell_commands=False` + `/clear` 或 `/help` | 正常执行（不受限） |

---

## 8. 与现有功能的兼容性

| 现有功能 | 关系 |
|---|---|
| `workspace_override` 字段 | **不变** — `/cd` 底层仍是设置此字段 |
| `AgentBridge.get_session()` | **不变** — `/cd` `/ls` `/clear` 都通过它获取 session |
| `AgentBridge.process()` overlay | **不变** — 还是读 `session.workspace_override` |
| `check_workspace()` | **不变** — 安全边界跟随 workspace_root |
| CLI REPL | **不变** — CLI 走自己的 `/` 处理逻辑 |
| 微信客服 channel | **不变** — 没这些指令入口 |
| `allow_workspace_switch` | **改名** `allow_shell_commands` — 语义不变 |

---

## 9. 与 `/workspace` 的差异对照

| | `/workspace`（旧） | `/cd`（新） |
|---|---|---|
| 绝对路径 | ✅ | ✅ |
| 相对路径 | ❌ | ✅ `os.path.join(current, arg)` |
| `..` 返回上级 | ❌ | ✅ + clamp 到原始 root |
| 无参显示当前 | ✅ | ✅（含 `(默认)` 标记当 workspace_root=None） |
| `--reset` | ✅ | ✅ |
| `clear_history()` | ✅ | ✅ |
| `/ls` 列出目录 | ❌ | ✅ 新增 |
| `/clear` 清历史 | ❌（`--reset` 间接可达） | ✅ 独立指令，始终可用 |
| `/help` | ❌ | ✅ 始终可用 |
| `workspace_root=None` fallback | 显示 "未设置（无限制）" | 回退 `os.getcwd()` + `(默认)` 标记 |
