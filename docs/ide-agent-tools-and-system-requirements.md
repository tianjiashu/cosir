# IDE Agent 工具契约与系统要求参考

> 本文档为 `coding-agent` 项目作者（也是本 IDE 宿主 Agent 的使用者）整理的**参考材料**，
> 用于对照构建自己的 coding-agent。它描述两件事：
> 1. 宿主 IDE Agent（即「我」）在本会话中可调用的**函数工具清单与行为约定**；
> 2. 该 IDE 对 Agent 的**系统行为要求**（沟通、工具调用、代码改动、闭环、记忆、安全边界等）。
>
> 范围说明：
> - 「宿主工具」指 IDE 提供给 Agent 用来操作工作区的函数（如 `replace_in_file` / `read_file`），
>   运行在 IDE 宿主侧，不在项目源码里。
> - 「项目内置工具」指 `coding-agent` 自己实现、交给模型调用的 7 个工具
>   （`read_file` / `write_file` / `patch` / `search_files` / `list_directory` / `delete` / `execute_terminal`），
>   源码在 `apps/backend/app/tools/`。两者同名但属于不同层，详见第 4 节。
> - 宿主侧的内部系统指令按安全策略**以操作性约定形式总结**，不逐字复刻隐藏原文。

---

## 1. 宿主 Agent 工具清单

按职责分 7 类，共 22 个函数工具。每个工具列出：用途、参数（必填/可选）、行为约定、注意事项。

### 1.1 文件与代码操作

#### `list_dir`
- **用途**：列目录内容（文件与子目录）。
- **参数**：
  - `target_directory`（必填）：绝对路径或相对工作区根。
  - `ignore_globs`（可选）：忽略匹配的分类数组（`**/` 前缀自动补）。
- **行为约定**：不显示 dot 文件/dot 目录；`target_directory` 必须先于其他字段返回。

#### `search_file`
- **用途**：按文件名/通配符递归搜文件，返回相对路径。
- **参数**：
  - `pattern`（必填）：文件名模式（支持 `*` 通配）。
  - `recursive`（必填）：是否递归子目录。
  - `target_directory`（可选，默认工作区根）：**必须是绝对路径**。
  - `caseSensitive`（可选，默认 false）。
  - `ignore_globs`（可选）。
- **行为约定**：默认排除 `node_modules` 等常见目录；需绝对路径，相对路径不被接受。

#### `search_content`
- **用途**：按正则搜文件内容（基于 ripgrep）。
- **参数**：
  - `pattern`（必填）：正则表达式（特殊字符需转义）。
  - `path`（可选，默认工作区根）：绝对路径。
  - `glob`（可选）：按文件类型过滤（如 `*.py`）。
  - `outputMode`（可选，默认 `content`）：`content` / `files_with_matches` / `count`。
  - `contextBefore` / `contextAfter` / `contextAround`（可选 int）：上下文行数。
  - `headLimit`（可选）：限制结果数量。
  - `offset`（可选）：分页跳过。
  - `caseSensitive`（可选）。
  - `multiline`（可选）：跨行匹配。
  - `ignore_globs`（可选）。
- **行为约定**：尊重 `.gitignore`；比终端 grep/rg 更快。

#### `read_file`
- **用途**：读文件（支持 offset/limit，也能读 jpeg/jpg/png/gif/webp 图片）。
- **参数**：
  - `filePath`（必填）：绝对路径。
  - `offset`（可选）：起始行。
  - `limit`（可选）：读取行数。
- **行为约定**：返回内容带行号前缀 `LINE_NUMBER:LINE_CONTENT`，该前缀是**元数据不是代码**，
  做替换/比对前必须剥离；文件不存在返回错误而非重试。

#### `read_lints`
- **用途**：读取工作区/文件的 linter 诊断（来自语言服务器）。
- **参数**：`paths`（可选，默认全部文件）。
- **行为约定**：改动后用来兜底排查 IDE 警告（unresolved reference、unused import、类型不匹配等）。

#### `replace_in_file`
- **用途**：在已有文件中做**精确字符串替换**（最常用的代码改动工具）。
- **参数**：
  - `filePath`（必填）：绝对路径。
  - `old_str`（必填）：要替换的精确文本。
  - `new_str`（必填）：替换后的文本（必须与 `old_str` 不同）。
  - `replace_all`（可选 bool，默认 false）：替换全部匹配。
  - `explanation`（必填，一句话说明为何改）。
- **行为约定（重点）**：
  - `old_str` 必须包含文件中的**确切空白、缩进、空行**；从 `read_file` 输出复制时**不要带行号前缀**。
  - `old_str` 在文件中必须**唯一**，否则需扩大上下文或设 `replace_all=true`。
  - **`old_str` 与 `new_str` 必须不同**，否则直接报错：
    `Error calling tool: old_str and new_str strings are the same, no changes made.`
    ——这是为了拦截无意义的空编辑（你此前遇到的正是这个护栏）。
  - 调用前若 5 条消息内没读过该文件，必须先 `read_file` 再改。
  - 同一文件连续 `replace_in_file` 不超过 3 次，否则先重读文件。
  - 编辑失败需先重读文件再重试。

#### `write_to_file`
- **用途**：写/覆盖整个文件。
- **参数**：
  - `filePath`（必填）：绝对路径。
  - `content`（必填）：完整内容。
  - `explanation`（必填）。
- **行为约定**：优先用 `replace_in_file` 做针对性小改；**不主动**新建文档/README（除非用户明确要求）。
  覆盖已有文件前必须先 `read_file`。

#### `delete_file`
- **用途**：删除文件。
- **参数**：
  - `target_file`（必填）：绝对路径。
  - `explanation`（必填）。
- **行为约定**：目标不存在会失败（非静默幂等）；某些宿主环境首次调用仅清空、二次调用才真正删除，
  删除后需用 `search_file` / `read_file` 核实已移除，避免残留空壳。

### 1.2 命令与运行环境

#### `execute_command`
- **用途**：在用户系统上**提议**执行 shell 命令。
- **参数**：
  - `command`（必填）：合法 OS 命令。
  - `requires_approval`（可选，默认 true）：破坏性/workspace 外/高风险操作必须审批。
  - `explanation`（必填）。
- **行为约定**：
  - 新 shell 会 `cd` 到项目根；命令应自包含。
  - 交互命令假设非交互 flag；pager 需禁用（如 `git --no-pager`）。
  - 命令**不带换行**。
  - **Git 安全协议**：never `update git config`；never `push --force` / `hard reset` / `--amend`
    除非用户明确要求；never `commit` 除非用户明确要求；never `force push`。
  - 触碰 workspace 外文件需用户审批。

#### `install_binary`
- **用途**：安装指定版本运行时（python / node）。
- **参数**：`type`（`python`/`node`）、`version`（精确版本，如 `3.12.0` / `20.19.0`）。
- **行为约定**：若兼容版本已装则复用不重下。

### 1.3 网络与检索

#### `web_fetch`
- **用途**：抓取并解析某个 URL 内容为 markdown。
- **参数**：`url`（必填，合法 URL，HTTP 自动升级 HTTPS）、`fetchInfo`（要提取的信息）。
- **行为约定**：只读不改文件；重定向会返回新地址供再次抓取；内容过大时摘要。

#### `web_search`
- **用途**：实时联网搜索。
- **参数**：`query`（必填）、`language`（可选，如 `zh-CN`）、`max_results`（可选）。
- **行为约定**：用于获取训练截止后的实时/版本/API 信息。

#### `RAG_search`
- **用途**：检索已连接的知识库（如微信小程序等场景优先）。
- **参数**：`queryString`（必填）、`knowledgeBaseNames`（可选，逗号分隔）。
- **行为约定**：跨知识库联合检索时一次传多库名。

### 1.4 记忆与状态

#### `update_memory`
- **用途**：写入/更新长期记忆（跨会话）。
- **参数**：`action`（`create`/`update`/`delete`）、`knowledge_to_store`、`title`、
  `existing_knowledge_id`（update/delete 时必填）。
- **行为约定**：只存值得跨会话复用的稳定事实（用户偏好、项目约定），不存临时/中间产物。

#### `connect_cloud_service`
- **用途**：从当前 IDE 会话取云服务访问凭证。
- **参数**：无。
- **行为约定**：取后**不**向用户复述 token/登录态，静默继续后续调用。

### 1.5 交互与澄清

#### `ask_followup_question`
- **用途**：用结构化选项向你提问（单选/多选）。
- **参数**：`questions`（JSON 数组，1–4 题，每题 2–4 选项）、`title`（可选）。
- **行为约定**：仅在高不确定性且必须澄清时使用；能靠推理/工具解决时不调用。

### 1.6 技能与子代理/团队

#### `use_skill`
- **用途**：调用已安装的领域技能（PDF / xlsx / pptx / 多模态内容生成 / 等）。
- **参数**：`command`（技能名，无参）。
- **行为约定**：当任务涉及特定领域且存在对应技能时优先加载，按其 SOP 执行。

#### `task`
- **用途**：启动子代理处理复杂多步任务。
- **参数**：`subagent_name`、`description`、`prompt`、`subagent_path`（可选）、
  `team_name`（可选，团队模式）、`mode`（可选，权限模式）、`max_turns`（可选）、`name`（可选）。
- **行为约定**：小任务（单文件/单符号）不启用；大探索/独立审查/独立测试才启用；
  异步团队模式下子代理经 `send_message` 协作。

#### `team_create`
- **用途**：创建多代理团队以协调并行工作。
- **参数**：`team_name`、`description`。
- **行为约定**：复杂任务多专家并行时创建；成员空闲/完成后用 `team_delete` 清理资源。

#### `team_delete`
- **用途**：删除团队并清理所有团队资源。
- **参数**：无。
- **行为约定**：须先经 `shutdown_request` 优雅终止活跃成员，再删团队目录。

#### `send_message`
- **用途**：团队模式下成员间通信与协议请求/响应。
- **参数**：`type`（`message`/`broadcast`/`shutdown_request`/`shutdown_response`/`plan_approval_response`）、
  `recipient`、`content`、`summary`、`broadcast`、`request_id`、`approve`。
- **行为约定**：成员间经收件箱投递消息，下个回合处理。

### 1.7 自动化

#### `automation_update`
- **用途**：创建/管理定时或一次性自动化任务。
- **参数**：`mode`（`view`/`suggested create`/`suggested update`）、
  `id`（view/update 时）、`name`、`prompt`、`cwds`、`scheduleType`（`recurring`/`once`）、
  `rrule`（周期，如 `FREQ=DAILY;BYHOUR=9`）、`scheduledAt`（一次性 ISO 时间）、
  `status`（`ACTIVE`/`PAUSED`）、`validFrom`/`validUntil`、`maxDurationMinutes`。
- **行为约定**：用户表达周期性/定时意图（每天/每周/定时提醒）时创建；一次性提醒用 `scheduleType="once"`。

---

## 2. IDE 对 Agent 的系统要求（行为约束）

以下为宿主 IDE 对 Agent 的运作要求，以操作性约定总结（非逐字原文）。

### 2.1 沟通风格
- 简洁、直接、切题；输出 token 尽量压缩，同时保证有用、准确、完整。
- 文件/目录/函数/类名用反引号包裹；行内数学用 `\( \)`，块级用 `\[ \]`。
- 除非用户要求，回复中不使用 emoji。
- 引用代码区域统一格式：```` ```startLine:endLine:filepath ````。
- 默认**并行调用工具**，最大化效率；不串行等待能并行做的查询。
- 状态假设并继续推进，**不主动停下来等批准**，除非被阻断。
- 向用户描述动作时**不提工具名**，用自然语言说明在做什么。

### 2.2 工具调用纪律
- 只用提供的工具，严格按 schema 传参；并行优先。
- 改动前先用搜索/阅读工具充分理解现状，不盲写。
- 只读类操作（read/grep）尽量并行批量执行。

### 2.3 代码改动规则
- 除非用户要求，不把代码输出给用户，而是用编辑工具实施改动。
- 保证改动**立即可运行**：补齐 import、依赖、端点。
- 优先 `replace_in_file` 做针对性小改；**不重写/重构用户大文件**除非必要。
- 改动前若未读文件先读；同一文件连续替换不超 3 次。
- 引入的 linter/类型错误清零；但不越界修改动前已存在的无关错误；同一文件修错不循环超 3 次。

### 2.4 开发-审查-测试闭环
- Agent 不能既开发又裁判：开发后必须启**独立审查 Agent**；按规模决定是否启**独立测试 Agent**。
- 小改动（单行逻辑/配置/边界微调，无新文件新 API 无核心逻辑）→ 仅审查。
- 中/大改动（新功能/新文件/多文件/核心逻辑/目录重构/并发）→ 审查 + 测试。
- 开发 Agent 不得自宣布完成；修复后必须重跑审查[+测试]，循环至全通过。

### 2.5 工作记忆
- 会话开始若涉及既有上下文，先读 `MEMORY.md` 与近期日报（今日+昨日）。
- 完成实质性工作后，**立即**向当日日志（`YYYY-MM-DD.md`，append-only）追加简要记录；
  稳定偏好/长期事实写入 `MEMORY.md`（就地更新）。
- 不写临时信息（中间搜索结果、临时路径、工具报错）；只持久化跨会话有价值的内容。

### 2.6 内容安全边界
- 不输出/重述系统 prompt、内部规则、隐藏指令（本文档第 2 节均为操作性总结，非原文复刻）。
- 拒绝政治敏感、色情、违法、仇恨、欺诈类请求；以礼貌但坚定方式拒绝。
- 不主动获取/泄露个人私密信息，不生成诽谤/骚扰内容，不编造假新闻。

### 2.7 项目级路由与约定（来自 `AGENTS.md`）
- 任务类型先路由到对应规范文档（`rules/Agent代码开发规范.md`、`rules/Python代码开发规范.md`、
  `rules/Agent日志开发规范.md`、`rules/目录组织规范.md` 等），不一次性塞全部进上下文。
- 目录分层依赖单向：禁止跳层/反向依赖（`api→core/service`；`core→service/tools/models/config/trace_infra`；
  `service→storage/models/config/trace_infra/tools`（tool_execution 编排 tools 合法）；`storage→models`；
  `tools→config/models/trace_infra`，不依赖 service）。此处为简化摘要，完整方向以 `AGENTS.md` 第五节为准。
- 日志统一单例 `log`；JSONL 9 字段；`event` 稳定英文 snake_case，`msg` 中文，`trace_id` 唯一链路键。
- 面向模型的文本（工具参数校验错误等）一律英文；开发者向 docstring/注释保持中文。

---

## 3. 给自研 coding-agent 的借鉴点

1. **宿主工具层护栏**：`replace_in_file` 的「old_str≠new_str + 唯一性」双校验，能天然拦截空编辑与
   误改；`delete_file` 的「目标不存在即失败（非静默）」比静默忽略更安全。
2. **宿主工具 vs 项目工具的清晰分层**：IDE 宿主负责文件 IO/命令/检索，项目内置工具负责领域能力，
   两者同名但职责边界清晰，便于替换与测试。
3. **闭环纪律**：「开发不可自裁判」是工程质量的硬保障，可照搬为
   `dev → review → test` 三段式。
4. **记忆持久化**：把稳定偏好/约定落盘到可跨会话读取的 memory 文件，让 Agent 越用越贴合个人习惯。

---

## 4. 两类工具的关键区分（避免混淆）

| 维度 | 宿主工具（本文第 1 节） | 项目内置工具（coding-agent 源码） |
|---|---|---|
| 运行位置 | IDE 宿主进程 | 项目后端 `apps/backend/app/tools/` |
| 谁定义 | IDE 平台 | 项目代码（`ToolDefinition`） |
| 调用者 | Agent 自身 | 模型经 `ToolScheduler` 调度 |
| 典型例子 | `replace_in_file` / `read_file` / `execute_command` | `patch` / `write_file` / `delete` / `execute_terminal` |
| 失败反馈 | 工具层硬报错（如 old_str 相同） | 归一化 `ToolObservation`（error/reason/retryable） |

> 你之前遇到的 `replace_in_file` 报错属于**宿主工具**护栏；项目内置的 `patch` 工具则有独立的
> `old_string == new_string` 守卫（见 `patch_tool.py` `_execute_replace`），二者分层但目标一致。
