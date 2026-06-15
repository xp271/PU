# Plan: 上下文富化 — 增加系统环境信息

## Context

`enrich_context()` 当前只注入日期和 git status，缺少 OS 和 shell 信息。DeepSeek 等模型默认假设 Linux 环境，导致生成的 shell 命令在 Windows 上直接失败（exit code 255）。

## 目标

在 system 提示中追加一段 `<environment>` XML 块，告知模型当前运行环境。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `src/agent_core/query/context.py` | `enrich_context()` 追加 `<environment>` 块 |
| `tests/unit/test_context.py` | 更新已有测试，验证新字段 |

## 实现细节

### `enrich_context()` 改动

在日期行之后、git status 之前插入：

```python
parts.append(
    "<environment>\n"
    f"  Platform: {sys.platform}\n"
    f"  Shell: {os.environ.get('COMSPEC', os.environ.get('SHELL', 'unknown'))}\n"
    f"  Workspace: {workspace_root or os.getcwd()}\n"
    "</environment>"
)
```

需要新增 `import os, sys`（标准库，无新依赖）。

**最终 system 结构**：

```
Today's date is 2026-06-15.

<environment>
  Platform: win32
  Shell: C:\Windows\System32\cmd.exe
  Workspace: C:\PU
</environment>

<git_status>
...
</git_status>
```

### Shell 检测逻辑

- Windows：读 `COMSPEC` 环境变量（默认 `cmd.exe`）
- Linux/macOS：读 `SHELL` 环境变量（默认 `/bin/bash`）
- 都取不到 → `"unknown"`

### Workspace

- 优先用传入的 `workspace_root`
- None 时用 `os.getcwd()`

## 测试

更新 `tests/unit/test_context.py`，验证：
- `enrich_context()` 输出包含 `<environment>` 标签
- 包含 `Platform:` 行
- workspace 正确

## 验证

```bash
python -m pytest tests/unit/test_context.py -v
python -m pytest tests/ -x -q
```
