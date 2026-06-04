# Agent 核心系统重构开发 TodoList (Python 技术栈)

> 每个条目预估纯手工开发时间约 2 天（16h），按依赖关系排序，前一项完成后下一项方可开始。

---

## 技术栈对照

| 原技术栈 (TypeScript) | Python 替代 |
|----------------------|------------|
| Bun / npm | uv + pip (pyproject.toml) |
| TypeScript + ESM | Python 3.12+ + type hints |
| Commander.js | Typer / Click |
| Zod | Pydantic v2 |
| Ink (React TUI) | Textual / Rich |
| Anthropic SDK (TS) | anthropic (Python SDK) |
| MCP SDK (TS) | mcp (Python SDK) |
| Tauri 2 + React | Textual 桌面模式 / PySide6 |
| Express / Bun.serve | FastAPI + uvicorn |
| WebSocket (Bun) | FastAPI WebSocket / websockets |
| ripgrep | ripgrep-py / subprocess |
| Vitest | pytest + pytest-asyncio |
| VitePress | MkDocs Material / Sphinx |
| tree-sitter (TS bindings) | tree-sitter Python bindings |
| node:child_process | asyncio.subprocess |
| GrowthBook (TS) | growthbook (Python) |
| OpenTelemetry (TS) | opentelemetry-api/sdk (Python) |

---

## Phase 1: 基础设施 (Week 1-2)

### 1. 项目脚手架 & CLI 入口
- [ ] 初始化 Python 项目：`pyproject.toml`（uv 管理），包结构布局（`src/agent/`）
- [ ] 实现 CLI 参数解析（Typer），支持 `--version`, `--model`, `--json`, `--resume` 等基础参数
- [ ] 快速路径路由：`--version` 直接退出，`daemon`, `remote-control`, `bridge` 等子命令分发到对应 handler
- [ ] `bin/agent-cli` 可执行入口脚本（console_scripts entry point）
- [ ] 早期输入缓冲机制，asyncio 事件循环初始化，防止 REPL 就绪前 stdin 数据丢失

### 2. 全局状态管理 & 配置系统
- [ ] `AppState` dataclass：session_id, cwd, project_root, is_interactive, model_usage, registered_hooks, total_cost_usd 等
- [ ] 全局 State 单例（`get_state()` / `set_state()` 线程安全访问）
- [ ] 配置加载链：`~/.agent/settings.json` → 项目 `.agent/settings.json` → 环境变量
- [ ] pydantic 配置模型 + JSON 序列化/反序列化，保留未知字段
- [ ] 安全环境变量注入（trust dialog 前后分离）
- [ ] 优雅关闭：`signal.signal(SIGINT/SIGTERM)` + asyncio 任务取消
- [ ] OpenTelemetry 初始化 + GrowthBook feature flag 集成

### 3. Tool 框架定义
- [ ] `Tool` 抽象基类：`call()` / `validate_input()` / `check_permissions()` / `is_enabled` / `is_concurrency_safe` / `is_read_only` / `is_destructive`
- [ ] `ToolUseContext` dataclass：options / abort_signal / read_file_state / get_app_state / messages / agent_id / query_tracking
- [ ] Tool 注册表（`dict[str, Tool]`），支持装饰器注册 + 运行时按名称查找
- [ ] Input/Output 使用 Pydantic v2 model 定义 + JSON Schema 自动生成
- [ ] `map_tool_result_to_block_param()` — 工具结果 → API tool_result block 序列化
- [ ] 工具 UI 渲染接口（`render_tool_use_message()` / `render_tool_result_message()`）

### 4. 只读工具实现（Glob + Grep + FileRead）
- [ ] **GlobTool**: 封装 `pathlib.glob` + `fnmatch`，支持 pattern + path，按 mtime 排序，默认上限 100 条
- [ ] **GrepTool**: 封装 ripgrep（`subprocess` 调用），支持 output_mode（files_with_matches/content/count），head_limit/offset 分页，context 上下文行，multiline 模式
- [ ] **FileReadTool**: 多格式文件读取（text/image/PDF），offset/limit 分段读取，token 预算校验，Pillow 图片处理，设备文件/二进制文件拦截，read_file_state 去重缓存

### 5. 写工具实现（FileEdit + FileWrite + Bash）
- [ ] **FileEditTool**: 搜索替换编辑（old_string → new_string），文件存在校验，read_file_state 过期检查，`difflib` 生成 unified diff，原子写入（tempfile + os.replace），编码检测（chardet），UNC 路径拦截
- [ ] **FileWriteTool**: 创建/覆盖文件，父目录自动创建（`pathlib.mkdir(parents=True)`），read_file_state 过期检查
- [ ] **BashTool**: `asyncio.create_subprocess_shell` 执行，跨平台 shell 选择（bash/zsh/cmd/pwsh），超时控制（`asyncio.wait_for`），沙箱执行支持，后台执行支持

### 6. LLM API 集成层
- [ ] Anthropic Messages API 封装（`anthropic` Python SDK + `AsyncAnthropic`）
- [ ] 消息规范化（`normalize_messages()`）：处理 system/user/assistant 角色交替，处理 tool_use/tool_result 配对
- [ ] 流式响应异步迭代器：content_block_start/delta/stop, message_start/delta/stop 事件解析
- [ ] 错误处理：max_output_tokens 恢复（最多 3 次重试），rate limit 指数退避（tenacity）
- [ ] API key 管理（环境变量 + 配置文件）+ 连接池预热

---

## Phase 2: Agent 核心循环 (Week 3-4)

### 7. Agent 主循环 (query 系统)
- [ ] `query()` 异步生成器：接受 messages + system_prompt + context，`AsyncGenerator[Message]`
- [ ] Turn 循环核心逻辑：
  - 组装上下文（git status via `subprocess`, 当前日期, AGENTS.md 等富化）
  - 发送 API 请求 → 流式接收 assistant message
  - 如有 tool_use → 分发到工具执行管线
  - tool_result 回填 → 继续循环（检查 token 预算）
- [ ] Token 预算管理：90% 阈值检测，"收益递减"检测（连续多轮 delta < 500 tokens），`max_turns_reached` 信号注入
- [ ] 自动压缩（auto-compact）：上下文超过阈值时触发对话压缩（调用 LLM 摘要）
- [ ] Stop hooks 集成：每轮 assistant message 后执行

### 8. 工具执行管线
- [ ] `run_tools()` — 高级编排器：
  - 按并发安全性将 tool_use 分组
  - 并发安全组 → `asyncio.gather()` 并行执行
  - 非并发工具 → 串行 `async for` 执行
- [ ] `run_tool_use()` — 单工具执行生命周期：
  - resolve_tool → validate_input → check_permissions
  - PreToolUse hooks → `tool.call()` → PostToolUse hooks
  - 结果持久化（超大结果分片存储到临时文件）
- [ ] `StreamingToolExecutor` — 流式并发工具执行器，支持 `discard()` 丢弃待执行工具，信号量控制并发数

### 9. 权限系统
- [ ] 权限规则引擎：
  - 精确匹配 → 前缀匹配 → 通配符匹配（`fnmatch`）→ 路径约束
  - mode-specific 规则 → read-only 规则 → fallback "ask"
- [ ] 权限规则持久化（settings.json 中的 permissions 段，pydantic model）
- [ ] Bash 工具专用权限：
  - tree-sitter Python bindings 解析 shell 命令 AST
  - 复合命令逐子命令检查（`&&`, `|`, `;`）
  - 异步轻量 LLM 分类器语义判断
- [ ] 权限 UI 渲染：AskUserQuestion 交互 / 确认对话框

### 10. MCP 协议集成
- [ ] MCP 客户端：stdio / SSE / WebSocket / StreamableHTTP transport（使用 `mcp` Python SDK）
- [ ] `connect_to_server(name, config)` → 创建 transport → MCP Client 连接
- [ ] `fetch_tools_for_client()` → 获取服务端工具列表，动态创建 Tool 子类实例（名称格式 `mcp__server__tool`）
- [ ] MCP 资源 & Prompt 获取
- [ ] OAuth 认证流程（Token 刷新、step-up 认证）
- [ ] MCP 配置来源：项目 `.mcp.json` → 全局配置 → 环境变量 → 插件
- [ ] 服务端工具与内置工具冲突检测与去重

### 11. Hooks 系统
- [ ] Hook 类型定义（Protocol/ABC）：PreToolUse / PostToolUse / Stop / SubagentStart / SessionStart / SessionEnd
- [ ] Hook 注册与注销：全局注册表（`dict[HookType, list[Hook]]`）+ 项目级注册表
- [ ] `run_pre_tool_use_hooks()` — 执行前钩子管线，可阻断（返回 False）
- [ ] `run_post_tool_use_hooks()` — 执行后钩子管线，可修改输出/添加上下文/阻断继续
- [ ] Stop hook 周期性评估（含 Goal 检测）

---

## Phase 3: 子Agent & 多Agent (Week 5-6)

### 12. Agent 定义系统
- [ ] `AgentDefinition` dataclass：agent_type, when_to_use, tools, disallowed_tools, source, base_dir, get_system_prompt, model, permission_mode, mcp_servers, hooks, skills, memory, isolation, background
- [ ] Agent 定义加载器：markdown frontmatter（python-frontmatter）→ JSON → 插件入口
- [ ] Agent 定义优先级链：user_settings > project_settings > local_settings > plugin > flag_settings > built-in
- [ ] 内置 Agent 注册：GeneralPurpose, Explore, Plan, StatuslineSetup

### 13. 子Agent 执行引擎 (run_agent)
- [ ] `run_agent()` 异步生成器：
  - agent_id 生成（uuid4）/ 模型解析 / 隔离 ToolUseContext 创建（`copy.deepcopy`）
  - Agent 专属 MCP 服务器初始化（与父Agent合并去重）
  - Agent 专属 System Prompt 构建
  - SubagentStart hooks 执行 + frontmatter skills 预加载
  - `resolve_agent_tools()` 工具池过滤（allow/disallow/wildcard 集合运算）
  - 调用 `query()` 执行主循环 + sidechain transcript 记录（JSONL append）
  - `finally` 清理：MCP 连接 / hooks / traces / todos / shells
- [ ] 消息 fork 处理（`filter_incomplete_tool_calls()`）

### 14. AgentTool 实现
- [ ] `AgentTool.call()` — 子Agent 创建入口：
  - 路由：team spawn → fork 路径 → 远程 → 标准子Agent
  - 异步/同步分发
  - 工作树隔离（git worktree 子进程操作）
- [ ] 同步Agent：`register_agent_foreground()` → 内联迭代 run_agent → completion
- [ ] 异步Agent：`register_async_agent()` → `asyncio.create_task(run_async_agent_lifecycle())` 后台执行
  - 进度追踪（token count, tool use count, activity descriptions）
  - 后台摘要服务（每 30s asyncio 定时 fork 生成进度摘要）
  - finalize_agent_tool → complete_async_agent → enqueue_agent_notification（XML）
- [ ] Agent 恢复：`resume_agent_background()` → 从磁盘 JSONL 加载 transcript → 重建状态 → 重新启动
- [ ] SendMessage 机制：父Agent 向已完成子Agent 发送后续消息

### 15. 多Agent 协调器 (Coordinator 模式)
- [ ] Coordinator 模式开关（`AGENT_COORDINATOR_MODE` 环境变量）
- [ ] Coordinator System Prompt（管理者的角色、工作流、决策框架）
- [ ] Research → Synthesis → Implementation → Verification 工作流
- [ ] Worker 工具池过滤（排除 TeamCreate/TeamDelete/SendMessage 等管理工具）
- [ ] 并行 Worker 管理 & 结果聚合（`asyncio.gather` + 超时处理）
- [ ] Fork Agent（继承父Agent上下文，共享 prompt cache）

---

## Phase 4: 通信与交付 (Week 7-8)

### 16. 传输层
- [ ] Transport 抽象基类：`connect()` / `on_data(callback)` / `write(data)` / `close()`
- [ ] `WebSocketTransport`：全双工（`websockets` 库），自动重连（指数退避 1s~30s，10min 预算），ping/pong 心跳（10s），keep-alive（5min）
- [ ] `SSETransport`：SSE 读（`aiohttp` / `httpx` 流式）+ POST 写，增量帧解析，45s 超时
- [ ] `HybridTransport`：WS 读 + POST 写，通过 SerialBatchEventUploader 批量上传
- [ ] `SerialBatchEventUploader`：串行 POST（`asyncio.Lock` 保证最多1个在途），事件批处理（100ms 窗口），指数退避重试

### 17. SDK 协议 (StructuredIO)
- [ ] NDJSON 消息解析器（`json.loads` 逐行）：user / control_request / control_response / update_environment_variables / keep_alive
- [ ] NDJSON 输出序列化器（`json.dumps` + `\n`）：stream_event / control_request / result
- [ ] `create_can_use_tool()` — 权限请求/响应双向通信：
  - 发送 `can_use_tool` control_request → 等待匹配 control_response
  - request_id（uuid4）管理 + `asyncio.wait_for` 超时取消
- [ ] pending_requests dict 管理（并发权限请求）

### 18. 本地服务器 & 协议代理
- [ ] FastAPI + uvicorn HTTP + WebSocket 服务器
- [ ] REST API 路由：`/api/sessions`, `/api/conversations`, `/api/settings`, `/api/models`, `/api/permissions`, `/api/agents`, `/api/tasks`, `/api/mcp`, `/api/skills` 等
- [ ] WebSocket endpoint：客户端连接 → 启动 CLI 子进程（stdout JSON 流模式）→ 双向消息转发
- [ ] 协议代理中间件：Anthropic Messages API → OpenAI Chat Completions API 格式转换
  - 请求转换（`anthropic_to_openai_chat()`）
  - 流式响应转换（`openai_chat_stream_to_anthropic()`）
- [ ] 多 Provider 配置管理（pydantic Settings）

### 19. 终端 UI (Textual)
- [ ] Textual App 主屏幕：对话区域 + 输入区域 + 状态栏
- [ ] 消息渲染 Widget：assistant 文本 / tool_use 块 / tool_result 块 / 权限对话框
- [ ] 权限确认流 UI（Bash/FileEdit/WebFetch 等各类权限请求组件）
- [ ] 进度指示器 & 后台任务状态栏 Footer
- [ ] 会话选择/恢复界面（DataTable + 搜索）

---

## Phase 5: 持久化 & 扩展 (Week 9-10)

### 20. 会话持久化 & 恢复
- [ ] 对话转录存储：JSONL 格式（`~/.agent/projects/**/*.jsonl`），使用 `aiofiles` 异步写入
- [ ] Sidechain transcript：子Agent 独立 JSONL 文件
- [ ] 会话列表 & 全文搜索（sqlite-utils / 简单的 JSON 索引）
- [ ] 会话恢复：加载历史消息 → 重建 ToolUseContext 状态 → 继续对话
- [ ] 向后兼容：格式迁移脚本 & 旧 fixture 回归测试（pytest）

### 21. Plugin & Skills 系统
- [ ] Plugin 注册表：`importlib.metadata.entry_points` 发现 + 内置插件
- [ ] Plugin 生命周期：enable / disable / reload（模块热重载）
- [ ] Skill 定义格式（SKILL.md frontmatter，python-frontmatter 解析）
- [ ] Skill 加载器：文件系统递归扫描 + MCP prompt 映射
- [ ] Skill 执行：inline 模式（展开 prompt + context_modifier）+ fork 模式（隔离子Agent）
- [ ] Skill 权限：deny/ask/allow 规则匹配（复用权限引擎）

### 22. 桌面端基础 (Textual 桌面模式 / PySide6)
- [ ] Textual 桌面 App 独立窗口运行（非终端模式）
- [ ] 或 PySide6 WebView 方案：FastAPI 后端 + Qt WebEngine 前端
- [ ] 多会话标签页工作台
- [ ] 会话管理面板（项目切换、分支/Worktree 选择）
- [ ] 本地服务器通信（WebSocket 连接 → 实时消息渲染）

---

## Phase 6: 外围扩展 (Week 11-12)

### 23. IM 适配器 & 远程访问
- [ ] 适配器框架：共享抽象基类 + 独立进程 sidecar
- [ ] Telegram 适配器：`python-telegram-bot` 消息接收/回复/权限审批
- [ ] 飞书 / 微信 / 钉钉 适配器（各自 SDK / Webhook 对接）
- [ ] H5 远程访问：FastAPI 静态页面 + 一次性令牌（JWT）→ 移动端 Web 接入

### 24. Computer Use & 定时任务
- [ ] 截屏服务（`Pillow.ImageGrab` / `mss`）+ 坐标映射
- [ ] 鼠标/键盘控制（`pyautogui` / `pynput`：click, type, scroll, drag）
- [ ] 应用白名单管理（platform-specific 进程检测）
- [ ] 定时任务系统：`croniter` Cron 表达式解析 + `APScheduler` 任务调度 + 执行结果通知

---

## 统计

| 阶段 | 条目数 | 预估天数 |
|------|--------|----------|
| Phase 1: 基础设施 | 6 | 12 |
| Phase 2: Agent 核心循环 | 5 | 10 |
| Phase 3: 子Agent & 多Agent | 4 | 8 |
| Phase 4: 通信与交付 | 4 | 8 |
| Phase 5: 持久化 & 扩展 | 3 | 6 |
| Phase 6: 外围扩展 | 2 | 4 |
| **总计** | **24** | **48 天 (~10 周)** |

---

## 核心依赖清单 (pyproject.toml)

```toml
[project]
name = "agent-cli"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    # CLI
    "typer>=0.15",
    "rich>=13",

    # 异步
    "aiofiles>=24",
    "aiohttp>=3",
    "websockets>=14",
    "httpx>=0.28",

    # 数据
    "pydantic>=2",
    "python-frontmatter>=1",

    # LLM
    "anthropic>=0.60",

    # MCP
    "mcp>=1",

    # 终端 UI
    "textual>=2",

    # 服务器
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",

    # 工具
    "pyautogui>=0.9",
    "mss>=10",
    "Pillow>=11",
    "croniter>=6",
    "APScheduler>=3",
    "tenacity>=9",
    "tree-sitter>=0.24",
    "python-telegram-bot>=22",

    # 可观测性
    "opentelemetry-api>=1.30",
    "opentelemetry-sdk>=1.30",
    "growthbook>=1",

    # 测试
    "pytest>=8",
    "pytest-asyncio>=0.25",
]

[project.scripts]
agent-cli = "agent.entrypoints.cli:main"
```

> 注：以上为单兵开发估算。多人并行开发可大幅缩短周期，但需注意模块间的依赖约束。
