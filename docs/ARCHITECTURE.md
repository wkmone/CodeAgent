# CodeAgent (codecli) 架构图

> Python 3.13 · typer + prompt-toolkit + rich + httpx + pillow
> 入口：`codecli` 命令（`codecli.entry.cli:app`）

## 1. 总体分层架构

```mermaid
flowchart TB
    USER(["用户终端"])

    subgraph ENTRY["① 入口层 entry/"]
        CLI["cli.py · Typer CLI<br/>--prompt / --mode react|plan|team<br/>--json / --plain / --model"]
        REPL["repl.py · 交互式 REPL<br/>slash 命令 /memory /model /skill /task /snapshot<br/>PermissionModeController · HITL 审批"]
        MS["model_selector.py<br/>交互式模型/供应商选择"]
    end

    subgraph BOOT["② 装配层"]
        BS["bootstrap.py<br/>build_tool_registry()"]
        CFG["config/config.py · load_config()<br/>~/.codecli + .codecli + 环境变量 合并<br/>Llm/Tools/Mcp/Memory/Policy/Prompt/Feature"]
        TYPES["types.py · Message / Usage / QueryResult"]
    end

    subgraph AGENT["③ Agent 核心 agent/"]
        AR["AgentRunner<br/>一次性调用门面 (ask/plan/team)"]
        AG["Agent<br/>模式分发 + ReAct 主循环<br/>快照 / 技能注入 / 用量与成本"]
        PE["PlanExecuteAgent<br/>规划 → 任务 DAG 执行"]
        ORCH["AgentOrchestrator<br/>team 模式：planner → 并行 workers → reviewer<br/>(SubAgent 队列 + 重试)"]
        Q["query.py<br/>共享 ReAct 循环 / 工具增量合并"]
    end

    subgraph PLAN["④ 规划 plan/"]
        PL["Planner<br/>LLM 生成任务 DAG (JSON)"]
        PM["models.py<br/>ExecutionPlan / Task / TaskType"]
    end

    subgraph LLM["⑤ LLM 层 llm/"]
        FAC["factory.py · create_llm_client()<br/>deepseek / openai / glm / kimi / step / 兼容"]
        OC["openai_compatible.py<br/>httpx SSE 流式客户端"]
        PROF["model_profiles.py<br/>ModelProfile + CustomModelStore"]
        PRICE["pricing.py<br/>价格档案 / calculate_cost"]
    end

    subgraph TOOLS["⑥ 工具层 tools/"]
        REG["registry.py · ToolRegistry"]
        EXE["executor.py · ToolExecutor<br/>只读工具并发(信号量) / 写工具串行<br/>HITL 审批决策"]
        BI["builtins.py<br/>read_file write_file edit_file list_dir glob<br/>grep directory_tree get_file_info bash<br/>web_search web_fetch save_memory search_memory<br/>load_skill save_skill search_code revert_turn"]
        FO["file_ops.py<br/>文件操作实现 (diff/截断/跳过规则)"]
        CMD["commands.py · CommandExecutor<br/>超时 / 截断 / 敏感命令分类"]
    end

    subgraph POLICY["⑦ 安全策略 policy/"]
        CG["CommandGuard<br/>危险命令黑名单 + 正则"]
        PG["PathGuard<br/>工作区路径围栏"]
        AUD["AuditLog<br/>JSONL 审计日志"]
    end

    subgraph CTX["⑧ 上下文与记忆"]
        CWM["context/manager.py<br/>ContextWindowManager<br/>确定性抽取式压缩 (ContextBudget)"]
        MEM["memory/manager.py<br/>MemoryManager · SQLite 长期记忆<br/>CJK 感知相关性评分"]
        RAG["rag/code_index.py<br/>CodeIndex · SQLite 代码索引检索"]
        PA["prompt/assembler.py<br/>PromptAssembler<br/>静态系统提示 + 动态提示 + 记忆召回"]
    end

    subgraph SKILL["⑨ 技能 skill/"]
        SR["SkillRegistry / SkillMatcher<br/>frontmatter 技能 · 候选匹配"]
        SCB["SkillContextBuffer<br/>load_skill 上下文注入"]
    end

    subgraph SNAP["⑩ 快照 snapshot/"]
        SS["SnapshotService<br/>pre-turn / post-turn 工作区快照<br/>~/.codecli/snapshots (revert_turn)"]
    end

    subgraph EXT["⑪ 扩展与运行时"]
        MCP["mcp/client.py<br/>MCP 客户端 (预留, 空实现)"]
        RT["runtime/ · RuntimeApiServer<br/>HTTP 任务 API + DurableTaskManager<br/>(SQLite 队列 / 租约)"]
        IMG["image/processor.py<br/>@图片引用 → base64 多模态 part"]
        WEB["web/ · fetch_url / search_web (stub)"]
        LSP["lsp/diagnostics.py (stub)"]
    end

    subgraph RENDER["⑫ 渲染层 render/"]
        RR["RichRenderer · 流式事件渲染<br/>banner / 工具栏 / 上下文占用条"]
        PR["PlainRenderer · 纯文本输出"]
    end

    LLMEXT[("外部 LLM API<br/>(OpenAI 兼容)")]
    FS[("本地文件系统")]
    SHELL[("Shell 环境")]
    SQLITE[("SQLite<br/>memory.db / code_index / runtime.db")]

    USER --> CLI
    CLI --> REPL
    REPL --> MS
    CLI --> BS
    CLI --> CFG
    BS --> REG
    CFG --> AGENT
    CLI --> AR
    REPL --> AG
    AR --> AG
    AR --> PE
    AR --> ORCH
    AG --> Q
    AG --> PE
    AG --> ORCH
    PE --> PL
    ORCH --> Q
    PL --> PM
    PL --> FAC
    AG --> FAC
    FAC --> OC
    FAC --> PROF
    OC --> LLMEXT
    OC --> PRICE

    Q --> EXE
    EXE --> REG
    EXE --> BI
    EXE --> AUD
    BI --> FO
    BI --> CMD
    FO --> PG
    CMD --> CG
    FO --> FS
    CMD --> SHELL
    EXE -.审批.-> REPL

    AG --> CWM
    AG --> PA
    AG --> SS
    AG --> SCB
    AG --> IMG
    PA --> MEM
    PA --> SR
    MEM --> SQLITE
    RAG --> SQLITE
    BI --> RAG
    BI --> MEM
    BI --> SR
    BI --> SS
    SS --> FS
    AGENT --> MCP
    RT --> AR
    RT --> SQLITE
    BI --> WEB
    Q --> LSP

    AG --> RENDER
    AR --> RENDER
    RENDER --> USER
    RR -.-> PR
```

## 2. ReAct 主循环（agent/agent.py + agent/query.py）

```mermaid
sequenceDiagram
    participant U as 用户 (REPL)
    participant A as Agent
    participant SK as SkillRegistry
    participant C as ContextWindowManager
    participant L as LlmClient (SSE)
    participant T as ToolExecutor
    participant P as Policy (Guard+HITL)

    U->>A: run(message)
    A->>A: SnapshotService.create("pre-turn")
    A->>SK: match(message) 候选技能注入
    A->>C: prepare(messages) 超预算则抽取式压缩
    loop 每轮 (max_turns)
        A->>L: chat(messages, tool_defs, system_prompt)
        L-->>A: text_delta / thinking_delta / tool_call_delta / usage
        A-->>U: 流式事件 (渲染层消费)
        alt stop_reason != tool_use
            A-->>U: done (usage + cost)
        else 有工具调用
            A->>T: execute_all(tool_calls)
            T->>P: PathGuard / CommandGuard / 审批
            P-->>T: allow / deny / skip (+AuditLog)
            T-->>A: ToolResult (只读并发, 写串行)
            A->>A: 结果写回 messages (技能上下文注入)
        end
    end
    A->>A: SnapshotService.create("post-turn")
```

## 3. 三种 Agent 模式

| 模式 | 实现 | 流程 |
|------|------|------|
| `react`（默认） | `Agent._run_react` / `query.query` | 标准 ReAct 循环：思考 → 工具 → 观察，直到模型停止 |
| `plan` | `PlanExecuteAgent` + `plan/Planner` | LLM 生成任务 DAG（JSON）→ 按依赖顺序执行任务 → 汇总 |
| `team` | `AgentOrchestrator` | planner 子代理出计划 → worker 队列并行执行步骤（可重试）→ reviewer 复核 |

## 4. 模块职责速查

| 模块 | 职责 |
|------|------|
| `entry/` | Typer CLI、REPL 循环、slash 命令、权限模式、模型选择 |
| `bootstrap.py` | 组装 ToolRegistry（内置工具 + MCP 预留） |
| `config/` | 三层配置合并：用户级 → 项目级 → 环境变量 |
| `agent/` | Agent / AgentRunner / PlanExecuteAgent / AgentOrchestrator / query |
| `plan/` | LLM 任务规划器与 ExecutionPlan 模型 |
| `llm/` | LLM 协议、多供应商工厂、SSE 流式客户端、模型档案、计价 |
| `tools/` | 工具定义/注册/执行、内置工具、文件操作、命令执行 |
| `policy/` | 命令守卫、路径守卫、审计日志（HITL 前置拦截） |
| `context/` | 上下文窗口预算与确定性历史压缩 |
| `memory/` | SQLite 长期记忆（存取 + 相关性召回） |
| `rag/` | 本地代码索引检索（search_code 工具后端） |
| `prompt/` | 系统提示静态/动态装配、记忆召回注入 |
| `skill/` | 技能注册/匹配/加载（frontmatter 文件） |
| `snapshot/` | 轮次前后工作区快照与回滚 |
| `mcp/` | MCP 客户端（预留，当前为空实现） |
| `runtime/` | HTTP 任务 API 服务 + SQLite 持久任务队列 |
| `image/` | 图片引用解析为多模态消息 |
| `web/` `lsp/` | 网络/诊断 stub |
| `render/` | rich / plain 双渲染器，消费流式事件 |

## 5. 外部依赖

- **运行时**：typer（CLI）、prompt-toolkit（REPL）、rich（渲染）、httpx（LLM HTTP/SSE）、pillow（图片）、sqlite3（记忆/索引/任务队列）
- **外部服务**：任意 OpenAI 兼容 LLM API（deepseek / openai / glm / kimi / step / 自定义 base_url）
- **本地状态目录**：`~/.codecli/`（config、memory.db、snapshots、runtime.db、技能）

## 备注（代码现状）

- `src/codecli/__main__.py` 中 `from entry.cli import app` 缺少 `codecli.` 前缀，直接 `python -m codecli` 会导入失败（console script 入口不受影响）。
- `mcp/client.py` 为 0 字节空文件，MCP 能力尚未实现（bootstrap 返回 `None` manager）。
- `config.py`（包根）与 `config/config.py` 内容重复，存在双份配置实现。
- `web/` 与 `lsp/` 目前是 stub。
