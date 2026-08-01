# CodeGraph 使用指南与 Agent 约束

本文档整理 CodeGraph 的本机用法、CLI 示例、Agent 使用规则和 CodeBuddy hook 强化方向。

信息来源：

- 本机 CodeGraph 版本：`1.4.1`
- 本机 CodeGraph 仓库：`/Users/woaigugu/Documents/codegraph`
- 主要参考文件：
  - `/Users/woaigugu/Documents/codegraph/README.md`
  - `/Users/woaigugu/Documents/codegraph/src/mcp/server-instructions.ts`
  - `/Users/woaigugu/Documents/codegraph/docs/design/agent-codegraph-adoption.md`
  - `codegraph help` 与各子命令 `codegraph help <command>`

## 1. 核心定位

CodeGraph 是一个本地代码知识图谱工具。它会把一个项目里的文件、符号、调用边、导入关系、继承关系等结构信息抽取到本地 SQLite 索引中。

Agent 使用 CodeGraph 的目标不是“多一个搜索命令”，而是替代低效的探索循环：

```text
grep / rg / find -> 读多个文件 -> 手工推断调用链 -> 再搜索 -> 再读文件
```

推荐改成：

```text
codegraph explore "具体问题/符号/调用流"
```

一次查询通常可以返回：

- 相关源码，带行号；
- 调用路径；
- 依赖它的代码，即 blast radius；
- 对当前修改应该验证的影响面提示。

CodeGraph 自己的 MCP 指令明确强调：对于已经有 `.codegraph/` 索引的代码，不要先用 grep/read 重建它已经算好的结构。

## 2. 安装与接入

### 2.1 安装 CLI

官方 README 提供两类安装方式。

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.ps1 | iex
```

已有 Node 时也可以用 npm：

```bash
npm i -g @colbymchenry/codegraph
```

### 2.2 接入 Agent

```bash
codegraph install
```

非交互安装常用：

```bash
codegraph install --target auto --location global --yes
```

安装命令参数：

```text
-t, --target <ids>      目标 Agent，逗号分隔，或 auto/all/none
-l, --location <where>  global 或 local
-y, --yes               非交互模式，默认 global + auto
--no-permissions        跳过 Claude Code auto-allow permissions
--print-config <id>     只打印某个 Agent 的 MCP 配置片段，不写文件
--refresh               刷新已有配置，不新增 Agent
```

### 2.3 初始化项目索引

在每个代码仓库根目录执行：

```bash
codegraph init
```

`init` 会创建 `.codegraph/` 并完成首次索引。一个全局 `codegraph install` 可以服务多个项目，但每个项目仍需要单独 `codegraph init`。

## 3. CLI 命令总览

```bash
codegraph                         # 运行交互式安装器
codegraph install                 # 接入支持的 Agent
codegraph uninstall               # 从 Agent 配置和 CLI 中移除 CodeGraph
codegraph init [path]             # 初始化项目并建立索引
codegraph uninit [path]           # 删除项目 .codegraph/ 索引
codegraph index [path]            # 全量重建索引
codegraph sync [path]             # 增量同步变更
codegraph status [path]           # 查看索引状态
codegraph unlock [path]           # 清理陈旧索引锁
codegraph query <search>          # 搜索符号
codegraph explore <query>         # 主查询入口，返回源码、调用路径、影响范围
codegraph node <symbol|file>      # 查看单个符号或文件
codegraph files [path]            # 从索引展示文件结构
codegraph callers <symbol>        # 查询谁调用了某个符号
codegraph callees <symbol>        # 查询某个符号调用了谁
codegraph impact <symbol>         # 分析修改某个符号的影响范围
codegraph affected [files...]     # 根据变更文件推断受影响测试
codegraph daemon                  # 管理后台 daemon
codegraph telemetry [on|off]      # 查看或切换匿名遥测
codegraph upgrade [version]       # 升级 CodeGraph
codegraph version                 # 打印版本
codegraph help [command]          # 查看帮助
```

## 4. 索引类命令

### 4.1 `codegraph init [path]`

用途：初始化项目并建立首次索引。

```bash
codegraph init
codegraph init /path/to/project
```

参数：

```text
-i, --index    兼容旧版本；现在 init 默认就会索引
-f, --force    即使路径看起来像 home/root 也继续初始化
-v, --verbose  输出详细 worker 生命周期和内存信息
```

Agent 规则：

- 如果项目没有 `.codegraph/`，先执行 `codegraph init`。
- 不要在未索引项目里假装可以用 CodeGraph。
- 初始化是用户项目写入操作，自动化 Agent 应说明会创建 `.codegraph/`。

### 4.2 `codegraph status [path]`

用途：查看索引健康状态。

```bash
codegraph status
codegraph status --json
```

参数：

```text
-j, --json  输出 JSON
```

当前项目示例输出要点：

```json
{
  "initialized": true,
  "version": "1.4.1",
  "fileCount": 342,
  "nodeCount": 3154,
  "edgeCount": 7332,
  "journalMode": "wal",
  "pendingChanges": {
    "added": 0,
    "modified": 0,
    "removed": 0
  },
  "index": {
    "state": "complete",
    "reindexRecommended": false
  }
}
```

Agent 规则：

- 会话开始、长时间运行后、或怀疑索引失效时，先看 `codegraph status`。
- `pendingChanges` 不为 0 时，先 `codegraph sync`。
- `reindexRecommended` 为 true 时，考虑 `codegraph index --force`。

### 4.3 `codegraph sync [path]`

用途：增量同步变更。

```bash
codegraph sync
codegraph sync --quiet
```

参数：

```text
-q, --quiet  减少输出，适合 git hook 或自动化脚本
```

Agent 规则：

- 修改文件后，如果继续依赖 CodeGraph 查询刚改过的区域，先执行 `codegraph sync`。
- MCP 模式有文件 watcher，但 CLI/hook 场景下显式 sync 更稳。

### 4.4 `codegraph index [path]`

用途：全量重建索引，相当于 fresh init 后的完整结果。

```bash
codegraph index
codegraph index --force
codegraph index --quiet
```

参数：

```text
-f, --force    即使路径看起来像 home/root 也继续索引
-q, --quiet    减少输出
-v, --verbose  输出详细 worker 生命周期和内存信息
```

使用时机：

- `status` 提示需要重建；
- 大规模迁移、重命名、生成文件策略变化后；
- 索引明显无法反映真实代码。

### 4.5 `codegraph unlock [path]`

用途：清理阻塞索引的陈旧 lock 文件。

```bash
codegraph unlock
```

使用时机：

- 索引异常中断后一直提示 lock；
- 确认没有另一个 CodeGraph 进程正在索引。

## 5. 查询类命令

### 5.1 `codegraph explore <query...>`

这是最重要的命令。用途是一次性探索一个区域、问题、符号或调用流。

```bash
codegraph explore "AgentRuntime 如何运行 workflow 并调用工具执行"
codegraph explore "从 tasks_api 到 AgentRuntime.run 的调用路径"
codegraph explore "tools/tool_execute 这一层如何执行工具并处理错误"
codegraph explore "RuntimeEvent payload models"
```

参数：

```text
-p, --path <path>      指定项目路径
--max-files <number>   限制返回源码的文件数量
```

适用场景：

- “这个模块怎么工作？”
- “请求如何从 API 到数据库？”
- “X 是怎么调用到 Y 的？”
- “准备修改某个功能，先看影响范围。”
- “debug 某个 bug 前，先找真实执行链路。”

当前项目真实示例：

```bash
codegraph explore "AgentRuntime 如何运行 workflow 并调用工具执行" --max-files 4
```

输出包含：

- `Found 14 symbols across 2 files`
- `Blast radius — what depends on these`
- `Source Code`
- 带行号源码块
- “不要再 Read 这些文件”的提示

Agent 规则：

- 对结构性问题，优先 `explore`。
- `explore` 返回的源码视为已经读过，不要马上再 `cat/sed/read_file`。
- 一次查询不够时，应该用更具体的问题再次 `explore`，而不是回到大范围 grep。

### 5.2 `codegraph query <search>`

用途：搜索符号候选。

```bash
codegraph query AgentRuntime
codegraph query runtime --limit 20
codegraph query AgentRuntime --kind class
codegraph query execute --json
```

参数：

```text
-p, --path <path>      指定项目路径
-l, --limit <number>   最大结果数，默认 10
-k, --kind <kind>      过滤符号类型，如 function/class/method
-j, --json             输出 JSON
```

适用场景：

- 只知道关键词，不知道准确函数/类名；
- 同名符号很多，需要先看候选；
- 写脚本自动查符号时使用 `--json`。

当前项目真实示例：

```bash
codegraph query AgentRuntime --limit 5
```

返回候选包括：

```text
class   AgentRuntime  apps/backend/app/core/runtime/runner.py:41
method  agent_registry
method  backend_health
function emit
method  cancel_turn
```

### 5.3 `codegraph node <symbol|file>`

用途：查看单个符号或文件。

```bash
codegraph node AgentRuntime
codegraph node "apps/backend/app/core/runtime/runner.py"
codegraph node "apps/backend/app/core/runtime/runner.py" --offset 80 --limit 80
codegraph node runner.py --symbols-only
```

参数：

```text
-p, --path <path>   指定项目路径
-f, --file <file>   文件模式，或在符号歧义时指定文件
--offset <number>   文件模式起始行，1-based
--limit <number>    文件模式最大行数
--symbols-only      只返回符号图和依赖，不返回完整源码
```

适用场景：

- 已经知道要看哪个文件；
- 已经知道要看哪个类/函数；
- 需要像 Read 一样读文件，但希望同时看到符号图和依赖方。

Agent 规则：

- 读源码文件前，优先 `codegraph node <file>`。
- 配置文件、文档、非索引文件再用普通 read/cat。

### 5.4 `codegraph files`

用途：从索引查看文件结构。

```bash
codegraph files
codegraph files --filter apps/backend/app/core
codegraph files --format flat --filter apps/backend/app/core --no-metadata
codegraph files --pattern "**/*.py" --format grouped
```

参数：

```text
-p, --path <path>      指定项目路径
--filter <dir>         只看某个目录下的文件
--pattern <glob>       glob 过滤
--format <format>      tree/flat/grouped，默认 tree
--max-depth <number>   tree 格式最大深度
--no-metadata          隐藏语言、符号数等元数据
-j, --json             输出 JSON
```

适用场景：

- 替代部分 `find` / `rg --files`；
- 想看“索引中有哪些文件”；
- 检查某个目录是否被 CodeGraph 纳入。

## 6. 调用关系和影响分析

### 6.1 `codegraph callers <symbol>`

用途：查谁调用某个函数/方法。

```bash
codegraph callers AgentRuntime
codegraph callers execute_tool --limit 50
codegraph callers RuntimeOperations --json
```

参数：

```text
-p, --path <path>      指定项目路径
-l, --limit <number>   最大结果数，默认 20
-j, --json             输出 JSON
```

适用场景：

- 修改函数前先查调用方；
- 删除/重命名前查影响；
- 排查“谁触发了这里”。

### 6.2 `codegraph callees <symbol>`

用途：查某个函数/方法调用了谁。

```bash
codegraph callees AgentRuntime.run
codegraph callees execute_tool --limit 50
```

参数：

```text
-p, --path <path>      指定项目路径
-l, --limit <number>   最大结果数，默认 20
-j, --json             输出 JSON
```

适用场景：

- 理解一个入口函数内部会触发哪些下游能力；
- 排查副作用；
- 评估测试覆盖面。

### 6.3 `codegraph impact <symbol>`

用途：分析修改某个符号会影响哪些代码。

```bash
codegraph impact AgentRuntime
codegraph impact RuntimeOperations --depth 3
codegraph impact ToolDefinition --json
```

参数：

```text
-p, --path <path>      指定项目路径
-d, --depth <number>   遍历深度，默认 2
-j, --json             输出 JSON
```

适用场景：

- 修改公共类、公共函数、协议类型前；
- 改 service/core/tools/models 等共享层前；
- 写测试计划前。

Agent 规则：

- 只要改公共符号，先 `impact`。
- 交付说明里应引用 `impact` 发现的验证范围。

### 6.4 `codegraph affected [files...]`

用途：根据变更文件推断受影响测试。

```bash
codegraph affected apps/backend/app/core/runtime/runner.py
git diff --name-only | codegraph affected --stdin
codegraph affected src/auth.ts --filter "e2e/*"
codegraph affected --stdin --quiet
```

参数：

```text
-p, --path <path>      指定项目路径
--stdin                从 stdin 读取文件列表
-d, --depth <number>   最大依赖遍历深度，默认 5
-f, --filter <glob>    自定义测试文件 glob
-j, --json             输出 JSON
-q, --quiet            只输出文件路径
```

适用场景：

- 修改完成后决定跑哪些测试；
- CI 或 git hook 自动选择测试；
- Agent 交付前做验证范围建议。

示例：

```bash
git diff --name-only | codegraph affected --stdin --quiet
```

## 7. MCP 用法

CodeGraph MCP 默认只暴露一个主工具：

```text
codegraph_explore
```

设计理由是：一个强工具比一组细工具更容易让 Agent 选对。`codegraph_explore` 可以覆盖：

- 架构问题；
- 模块解释；
- bug 定位；
- 文件/符号读取；
- 调用流追踪；
- blast radius 分析。

其他工具仍存在，但默认不暴露：

```text
codegraph_node
codegraph_search
codegraph_callers
codegraph_callees
codegraph_impact
codegraph_files
codegraph_status
```

如需重新暴露，可以设置：

```bash
CODEGRAPH_MCP_TOOLS=explore,node,search,callers
```

对于 CodeBuddy 当前场景，因为没有官方 CodeGraph MCP 适配，推荐先使用 CLI hook 约束，而不是依赖 MCP。

## 8. Agent 使用规则

### 8.1 什么时候必须用 CodeGraph

以下动作前必须优先考虑 CodeGraph：

- 理解代码结构；
- 定位实现位置；
- 追踪调用链；
- 阅读陌生源码文件；
- 准备修改已有代码；
- 修改公共函数、公共类、公共类型、公共服务；
- 分析 bug；
- 审查影响范围；
- 设计验证范围；
- 判断应该跑哪些测试。

### 8.2 推荐决策树

```text
任务是否涉及代码结构/调用/修改？
  否 -> 不强制使用 CodeGraph
  是 -> 项目是否有 .codegraph/？
    否 -> 询问或执行 codegraph init（取决于权限）
    是 -> codegraph status
      pending/reindex -> sync 或 index
      healthy -> 选择查询：
        不知道准确符号 -> query
        解释模块/流程 -> explore
        已知文件/符号 -> node
        查上游调用 -> callers
        查下游调用 -> callees
        改动影响 -> impact
        测试范围 -> affected
```

### 8.3 反模式

这些想法出现时，应停下来先用 CodeGraph：

- “我先 rg 一下。”
- “我先打开几个文件看看。”
- “这个改动很小，不用查。”
- “刚才已经用过一次 CodeGraph。”
- “我大概知道在哪。”
- “让子任务先探索代码。”

关键约束：

- CodeGraph 不是每个会话用一次，而是每次进入新的代码区域、符号、调用链或影响面时重新使用。
- 首次 `codegraph explore` 不能抵消后续所有代码探索。
- 如果 `explore` 返回的文件已经包含行号源码，不要立刻用 read/cat/sed 重读同一文件。
- 若需要更多上下文，先用更具体的 `explore` 或 `node --offset --limit`。

### 8.4 什么时候可以不用

以下场景可直接用普通工具：

- 读 Markdown 文档；
- 读配置文件；
- 读 `.env.example` 一类非源码文件；
- 查看 git diff；
- 查看测试输出、日志输出；
- CodeGraph 明确提示项目未索引，且用户不想初始化；
- 需要验证磁盘真实内容，而 CodeGraph 输出提示某文件 stale。

## 9. CodeBuddy Hook 强化建议

当前 CodeBuddy 适配是 SessionStart 注入。问题是：只靠启动提示，Agent 可能用一次后就回到普通 grep/read。

更强约束建议：

### 9.1 SessionStart

注入完整规则：

- 如何探测 CodeGraph CLI；
- 没有 `.codegraph/` 时如何 init；
- 每个 CLI 命令怎么用；
- 反模式；
- “不是用一次，而是每次代码探索前重新判断”。

### 9.2 PreCompact

上下文压缩前保留短规则：

```text
保留 CodeGraph 规则：
- 当前项目使用 CodeGraph CLI。
- 每次理解/定位/修改/审查代码前先用 CodeGraph。
- 新代码区域、新符号、新调用链、新影响范围要重新查询。
- 不要因为本会话已经用过一次 CodeGraph 就跳过。
```

### 9.3 PreToolUse

如果 CodeBuddy 支持 `PreToolUse`，建议对这些工具加软拦截：

- `rg`
- `grep`
- `find`
- `cat`
- `sed`
- `read_file`
- `list_directory`

软拦截逻辑：

```text
如果目标是源码文件或代码目录，且项目存在 .codegraph/：
  注入提醒：先用 codegraph explore/node/files。
如果目标是文档、配置、日志、测试输出：
  放行。
```

不要默认硬拦截。CodeGraph 自己的设计文档记录过，强制阻止 Read/Grep 可能导致 Agent 绕路，反而增加工具调用和耗时。优先做“强提醒 + 明确替代命令”。

### 9.4 建议注入模板

```text
CodeGraph 使用不是一次性义务。

每次需要理解代码、定位实现、追踪调用链、修改已有逻辑、审查影响范围、决定测试范围时，都必须重新判断是否需要 CodeGraph。

如果即将执行 grep/rg/find/cat/sed/read_file/list_directory 来探索源码，先停下：
- 不知道符号：codegraph query "<关键词>"
- 理解区域：codegraph explore "<问题/模块/调用流>"
- 已知文件：codegraph node "<文件路径>"
- 修改公共符号：codegraph impact "<符号>"
- 决定测试：git diff --name-only | codegraph affected --stdin

不要用“本会话已经用过一次 CodeGraph”作为跳过理由。
```

## 10. 故障处理

### 10.1 未初始化

现象：

```text
CodeGraph not initialized
```

处理：

```bash
codegraph init
```

### 10.2 索引慢

检查：

- `node_modules`、`dist`、构建产物是否被排除；
- 是否在网络盘、WSL `/mnt` 或非本地磁盘；
- 是否需要 `--quiet` 减少输出开销。

### 10.3 database is locked

README 说明当前版本使用 WAL，正常不应阻塞读写。如果遇到：

```bash
codegraph status
```

检查 `journalMode` 是否为 `wal`。如果不是，优先把项目和 `.codegraph/` 移到本地磁盘。

### 10.4 MCP server 不连接

检查：

```bash
codegraph status
codegraph install --refresh
```

MCP 场景下，不需要手动启动 server，Agent 会自己启动。

### 10.5 WSL / Windows 共享目录

不要让 Windows 和 WSL 共用同一个 `.codegraph/`。SQLite 锁和后台 server 与写入它的操作系统相关。

可用不同目录：

```bash
CODEGRAPH_DIR=.codegraph-win
```

### 10.6 缺少符号

处理顺序：

```bash
codegraph sync
codegraph status
codegraph index --force
```

还要确认：

- 语言受支持；
- 文件没有被 `.gitignore` 或默认排除规则跳过；
- 文件保存后等待一两秒再查。

## 11. 在本项目中的推荐用法

本项目已经存在 `.codegraph/`，当前 `codegraph status --json` 显示：

```text
initialized=true
version=1.4.1
fileCount=342
nodeCount=3154
edgeCount=7332
state=complete
pendingChanges=0
```

常用查询：

```bash
# 理解 runtime
codegraph explore "AgentRuntime 如何运行 workflow 并调用工具执行" --max-files 4

# 找 AgentRuntime 符号
codegraph query AgentRuntime --limit 5

# 查看 core 文件结构
codegraph files --filter apps/backend/app/core --format flat --no-metadata

# 查看 runner.py
codegraph node "apps/backend/app/core/runtime/runner.py"

# 改 RuntimeOperations 前看影响
codegraph impact RuntimeOperations --depth 3

# 修改完成后推断测试
git diff --name-only | codegraph affected --stdin --quiet
```

建议本项目 Agent 规则：

- 只要进入 `apps/backend/app/core`、`apps/backend/app/tools`、`apps/backend/app/service`、`apps/backend/app/storage`，先用 CodeGraph。
- 修改公共 dataclass、API schema、工具契约、runtime event payload 前，先 `impact`。
- 修改后继续查同一区域前，先 `codegraph sync`。
- 最终交付时说明 CodeGraph 用于定位了哪些符号或影响范围。

## 12. 后续可落地任务

1. 强化现有 CodeBuddy CodeGraph hook：
   - 加入 PreCompact；
   - 如果 CodeBuddy 支持 PreToolUse，增加 grep/read 软提醒；
   - 注入“不是用一次”的明确规则。

2. 为本项目新增 Agent 规则片段：
   - 开发场景必须先用 CodeGraph；
   - 不允许基于 grep/read 手工重建已有索引结构；
   - 修改公共符号前必须 `impact`。

3. 把 CodeGraph 和日志排查结合：
   - CodeGraph 用来定位代码路径；
   - 结构化日志/trace 用来验证真实运行路径；
   - Agent 排障流程应变成“先日志证据，再 CodeGraph 定位，再最小修改”。
