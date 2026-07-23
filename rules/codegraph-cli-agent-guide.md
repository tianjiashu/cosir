# CodeGraph CLI 面向 Agent 使用说明

> 本文面向在代码仓库中工作的 Agent，重点说明 CodeGraph CLI 的安装、初始化、查询、影响分析和维护命令。本文不展开 MCP、编辑器插件或图形界面集成。

## 1. CodeGraph 是什么

CodeGraph 是一个本地代码知识图谱工具。它会在项目中建立 `.codegraph/` 索引目录，记录文件、符号、调用关系、依赖关系和影响范围。Agent 在理解代码、定位实现、评估修改影响时，优先查询 CodeGraph，可以减少大范围 `grep`、逐文件读取和手工重建调用链。

Agent 使用 CodeGraph 的核心目标不是“搜索文本”，而是快速回答这几类问题：

- 这个功能在哪里实现？
- 某个函数、类、组件的源码和调用路径是什么？
- 修改某个符号会影响哪些调用方？
- 某个文件变化后，应该优先跑哪些测试？
- 当前仓库是否已经有可用索引？

## 2. 安装原则

项目初始化时，Agent 应先检查本机是否有 `codegraph` 命令；没有就安装。安装 CLI 只负责让本机可执行 `codegraph`，项目索引仍需要进入仓库后单独执行 `codegraph init`。

### 2.1 检查是否已安装

```bash
codegraph version
```

或：

```bash
command -v codegraph
```

如果命令存在，再检查当前版本：

```bash
codegraph version
```

### 2.2 macOS / Linux 安装

```bash
curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh
```

安装脚本会下载适合当前系统的独立构建。安装后如果当前 shell 仍找不到 `codegraph`，打开一个新终端，或重新加载 shell 配置。

### 2.3 Windows PowerShell 安装

```powershell
irm https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.ps1 | iex
```

### 2.4 npm 安装方式

如果机器已有 Node/npm，也可以使用 npm 安装：

```bash
npm i -g @colbymchenry/codegraph
```

Agent 选择安装方式时应优先遵守项目和机器已有工具链，不要为了安装 CodeGraph 改写项目依赖树。全局 npm 安装不应在项目目录里生成或修改锁文件。

### 2.5 升级

检查是否有新版本：

```bash
codegraph upgrade --check
```

升级到最新版：

```bash
codegraph upgrade
```

安装指定版本：

```bash
codegraph upgrade <version>
```

强制重装当前目标版本：

```bash
codegraph upgrade --force
```

## 3. 项目初始化流程

进入项目根目录后，按下面顺序执行。

### 3.1 判断项目是否已有索引

```bash
test -d .codegraph && echo "indexed" || echo "not indexed"
```

如果已有 `.codegraph/`，再看索引状态：

```bash
codegraph status
```

如果没有 `.codegraph/`，初始化：

```bash
codegraph init
```

`codegraph init` 会创建 `.codegraph/` 并构建初始索引。不要把 `.codegraph/` 当作源码修改；它是本地索引产物。

### 3.2 指定项目路径初始化

```bash
codegraph init /path/to/project
```

### 3.3 强制初始化

```bash
codegraph init --force /path/to/project
```

`--force` 用于路径看起来像 home 目录或文件系统根目录时强制执行。Agent 不应随意对大范围目录使用它，除非用户明确确认目标路径。

### 3.4 详细初始化日志

```bash
codegraph init --verbose
```

用于诊断索引慢、内存异常或 worker 生命周期问题。

## 4. Agent 使用总则

### 4.1 什么时候必须先用 CodeGraph

仓库根目录存在 `.codegraph/` 时，Agent 在以下场景应先用 CodeGraph，再精读必要源码：

- 需要理解模块、组件、函数、类、路由、服务或调用流程。
- 需要定位某个符号或文件。
- 修改代码前需要判断影响范围。
- 修复 bug 前需要找到入口、调用方、被调方。
- 代码审查时需要判断变更会影响哪些依赖。
- 选择测试范围时需要找受影响测试。

### 4.2 什么时候可以不用 CodeGraph

- 仓库没有 `.codegraph/`，且用户没有要求构建索引。
- 只是读取一个已知配置文件、文档或小文件。
- 执行简单命令，比如查看当前时间、列目录。
- CodeGraph 明确不可用，且当前任务需要继续推进。

### 4.3 查询后仍要回到源码和测试

CodeGraph 用于缩小范围和建立结构视角，不替代最终判断。涉及行为、边界条件、配置、测试断言、运行错误时，仍要读取源码、跑测试或查看日志。

## 5. 常用命令速查

| 命令 | 主要用途 | Agent 典型用法 |
| --- | --- | --- |
| `codegraph version` | 查看 CLI 版本 | 判断是否已安装 |
| `codegraph status` | 查看索引状态和统计 | 任务开始前确认索引可用 |
| `codegraph init` | 初始化项目并构建索引 | 新项目首次接入 |
| `codegraph index` | 全量重建索引 | 索引异常或配置变化后重建 |
| `codegraph sync` | 增量同步变更 | 修改后刷新索引 |
| `codegraph files` | 从索引查看文件结构 | 了解目录和语言分布 |
| `codegraph query` | 搜索符号 | 找函数、类、组件、接口 |
| `codegraph explore` | 一次性探索相关源码、调用链、影响范围 | 首选理解命令 |
| `codegraph node` | 查看单个符号或文件的源码与关系 | 精读某个节点 |
| `codegraph callers` | 查调用某符号的代码 | 改函数前看上游影响 |
| `codegraph callees` | 查某符号调用了什么 | 理解函数内部依赖 |
| `codegraph impact` | 分析修改某符号的影响范围 | 改共享函数/组件前必用 |
| `codegraph affected` | 根据变更文件找受影响测试 | 选择测试范围 |
| `codegraph unlock` | 清理陈旧锁 | 索引被 stale lock 卡住时使用 |
| `codegraph daemon` | 管理后台 daemon | 诊断后台进程问题 |
| `codegraph telemetry` | 查看或修改匿名统计开关 | 按用户偏好关闭统计 |
| `codegraph uninit` | 删除项目 `.codegraph/` | 用户明确要求移除索引时使用 |

## 6. 核心工作命令

### 6.1 `codegraph status`

用途：确认当前项目是否已初始化、索引是否最新、索引规模和语言分布。

```bash
codegraph status
```

指定路径：

```bash
codegraph status /path/to/project
```

Agent 使用场景：

- 接手项目后先确认索引状态。
- 修改后确认索引是否 up to date。
- 判断当前项目里有哪些语言、多少文件、多少节点和边。

如果提示未初始化，执行：

```bash
codegraph init
```

### 6.2 `codegraph init`

用途：初始化项目并构建完整索引。

```bash
codegraph init
```

常用选项：

```bash
codegraph init --verbose
codegraph init --force /path/to/project
```

Agent 使用场景：

- 项目根目录没有 `.codegraph/`。
- 用户明确要求“构建索引”。
- 新 clone 的仓库第一次使用 CodeGraph。

注意：

- 不要对用户 home 目录或磁盘根目录随意初始化。
- 初始化会创建 `.codegraph/`，这是本地索引目录。
- 初始化后用 `codegraph status` 确认结果。

### 6.3 `codegraph index`

用途：从头全量重建索引，效果等同于重新初始化后的新索引。

```bash
codegraph index
```

常用选项：

```bash
codegraph index --quiet
codegraph index --verbose
codegraph index --force /path/to/project
```

Agent 使用场景：

- 索引明显异常。
- 修改 `codegraph.json` 的 include/exclude/extensions 后需要重建。
- 大量移动、重命名文件后想要干净重建。

优先级：

- 普通修改后优先用 `codegraph sync`。
- 只有增量同步不能解决时，再用 `codegraph index`。

### 6.4 `codegraph sync`

用途：同步上次索引后的文件变化。

```bash
codegraph sync
```

常用选项：

```bash
codegraph sync --quiet
codegraph sync /path/to/project
```

Agent 使用场景：

- 修改代码后继续依赖 CodeGraph 查询。
- 新增、删除、重命名文件后刷新索引。
- 交付前确认索引没有滞后。

经验规则：

- 修改源码后，如果后续还要用 `explore`、`node`、`impact`，先跑 `codegraph sync`。
- 如果 CodeGraph 有自动同步，也可以等待几秒；但关键交付前建议显式同步一次。

## 7. 理解代码命令

### 7.1 `codegraph explore`

用途：按自然语言、文件名、符号名或流程问题探索代码区域。它会返回相关符号源码、调用路径和影响范围摘要，是 Agent 最常用命令。

```bash
codegraph explore "InputBar useTask useSSE 发送消息流程"
```

限制返回源码文件数：

```bash
codegraph explore --max-files 8 "AgentRuntime run turn stream"
```

指定项目路径：

```bash
codegraph explore --path /path/to/project "TaskService create_task"
```

Agent 使用场景：

- 问“某功能怎么实现？”
- 问“X 是如何到达 Y 的？”
- 改代码前找入口、调用链、相关文件。
- 审查代码时快速看到调用关系和影响面。

查询写法建议：

- 具体写出符号名、文件名、模块名。
- 流程问题写出起点和终点，例如：`createTurn useSSE RuntimeEvent task status`。
- 不要只写泛泛的“看看客户端”。

### 7.2 `codegraph node`

用途：查看单个符号的源码、调用方/被调方线索，或按文件模式读取文件的带行号内容和依赖信息。

查看符号：

```bash
codegraph node useTask
```

查看指定文件：

```bash
codegraph node --file apps/desktop/src/hooks/useTask.ts
```

查看文件某段：

```bash
codegraph node --file apps/desktop/src/hooks/useTask.ts --offset 60 --limit 120
```

只看符号地图和依赖：

```bash
codegraph node --file apps/desktop/src/hooks/useTask.ts --symbols-only
```

Agent 使用场景：

- 已知道目标符号，想快速精读。
- 需要带行号源码，方便后续精确修改或审查。
- 文件较大时只读取相关段落。

### 7.3 `codegraph files`

用途：从索引查看项目文件结构，而不是直接扫磁盘。

```bash
codegraph files
```

过滤目录：

```bash
codegraph files --filter apps/desktop/src
```

按 glob 过滤：

```bash
codegraph files --pattern "**/*.tsx"
```

控制输出格式：

```bash
codegraph files --format flat
codegraph files --format grouped
codegraph files --max-depth 3
codegraph files --no-metadata
```

输出 JSON：

```bash
codegraph files --json
```

Agent 使用场景：

- 初步了解项目目录。
- 找某类文件，例如所有 TSX 组件。
- 在不大范围 `find` 的情况下获得索引视角。

### 7.4 `codegraph query`

用途：搜索符号。适合知道大概名称，但不知道具体文件位置的情况。

```bash
codegraph query useTask
```

限制数量：

```bash
codegraph query task --limit 20
```

按节点类型过滤：

```bash
codegraph query TaskService --kind class
codegraph query createTask --kind function
```

输出 JSON：

```bash
codegraph query createTask --json
```

Agent 使用场景：

- 先找符号，再用 `codegraph node` 或 `codegraph explore` 精读。
- 名称冲突时列出候选。
- 自动化脚本需要结构化结果时用 `--json`。

## 8. 调用关系与影响分析命令

### 8.1 `codegraph callers`

用途：查谁调用了某个符号。

```bash
codegraph callers createTurn
```

限制数量：

```bash
codegraph callers createTurn --limit 50
```

输出 JSON：

```bash
codegraph callers createTurn --json
```

Agent 使用场景：

- 修改函数签名前看调用方。
- 删除或重命名符号前确认影响。
- 审查某个 API 是否被多个入口复用。

### 8.2 `codegraph callees`

用途：查某个符号内部调用了什么。

```bash
codegraph callees useTask
```

Agent 使用场景：

- 理解函数内部依赖。
- 判断某段逻辑是否跨层调用。
- 查找某个入口最终会触发哪些服务、store 或 API。

### 8.3 `codegraph impact`

用途：分析修改某个符号可能影响哪些代码。

```bash
codegraph impact useTask
```

控制遍历深度：

```bash
codegraph impact useTask --depth 3
```

输出 JSON：

```bash
codegraph impact useTask --json
```

Agent 使用场景：

- 改共享 hook、service、store、工具函数前。
- 判断是否需要扩大测试范围。
- 代码审查时确认 blast radius。

经验规则：

- 修改公共符号前，先跑 `impact`。
- 影响范围大时，不要只跑一个局部测试。
- `impact` 是静态分析结果，不代表运行时一定覆盖所有动态路径。

### 8.4 `codegraph affected`

用途：根据变更源文件找可能受影响的测试文件。

```bash
codegraph affected apps/desktop/src/hooks/useTask.ts
```

多个文件：

```bash
codegraph affected apps/desktop/src/hooks/useTask.ts apps/desktop/src/stores/taskStore.ts
```

从 stdin 读取文件列表：

```bash
git diff --name-only | codegraph affected --stdin
```

只输出路径：

```bash
git diff --name-only | codegraph affected --stdin --quiet
```

自定义测试文件 glob：

```bash
codegraph affected apps/desktop/src/hooks/useTask.ts --filter "src/**/*.test.ts*"
```

Agent 使用场景：

- 修改后决定先跑哪些测试。
- 大仓库中避免盲目全量测试。
- PR 审查时估计测试覆盖。

注意：

- `affected` 帮助选择测试范围，不替代必要的全量验证。
- 核心共享模块、跨端契约、构建配置变更后，仍应跑更完整的测试和构建。

## 9. 维护与排障命令

### 9.1 `codegraph unlock`

用途：删除阻塞索引的陈旧 lock 文件。

```bash
codegraph unlock
```

指定路径：

```bash
codegraph unlock /path/to/project
```

Agent 使用场景：

- `init`、`index`、`sync` 提示 stale lock。
- 上一次索引进程异常退出后无法继续。

注意：

- 只有确认没有正在运行的索引进程时再用。
- 不要把它当作普通同步命令。

### 9.2 `codegraph daemon`

用途：查看和管理 CodeGraph 后台 daemon，可交互选择停止。

```bash
codegraph daemon
```

Agent 使用场景：

- 怀疑后台 daemon 异常。
- 多个项目或多个会话导致后台状态混乱。

### 9.3 `codegraph telemetry`

用途：查看或修改匿名使用统计开关。

查看状态：

```bash
codegraph telemetry status
```

关闭：

```bash
codegraph telemetry off
```

开启：

```bash
codegraph telemetry on
```

Agent 使用原则：

- 用户未要求时，不主动改变 telemetry 设置。
- 用户要求隐私优先或关闭统计时，执行 `codegraph telemetry off`。

### 9.4 `codegraph uninit`

用途：从项目移除 CodeGraph 索引目录，即删除 `.codegraph/`。

```bash
codegraph uninit
```

跳过确认：

```bash
codegraph uninit --force
```

Agent 使用原则：

- 这是删除项目本地索引的操作，只有用户明确要求时执行。
- 不要为了解决普通同步问题直接 `uninit`；优先 `sync`、`index`、`unlock`。

## 10. 配置文件 `codegraph.json`

CodeGraph 默认零配置。通常不需要创建配置文件。只有以下情况才考虑在项目根目录添加 `codegraph.json`。

### 10.1 排除已提交但不该索引的目录

```json
{
  "exclude": ["static/", "**/vendor/**"]
}
```

适合场景：

- 仓库提交了大体积第三方主题、SDK、生成物。
- `.gitignore` 无法排除已经被 Git 跟踪的目录。

### 10.2 强制纳入被忽略的真实源码

```json
{
  "include": ["Tools/", "Local/typescript/"]
}
```

适合场景：

- 某些真实源码因为项目特殊原因被 `.gitignore` 忽略。
- 子项目来自其他版本控制系统，但仍需要被 CodeGraph 分析。

### 10.3 自定义扩展名语言映射

```json
{
  "extensions": {
    ".dota_lua": "lua",
    ".tpl": "php"
  }
}
```

修改 `codegraph.json` 后，建议全量重建：

```bash
codegraph index
```

## 11. Agent 推荐工作流

### 11.1 接手一个代码任务

```bash
codegraph status
```

如果未初始化：

```bash
codegraph init
```

然后用 `explore` 理解目标区域：

```bash
codegraph explore "要修改的功能、文件名、符号名"
```

### 11.2 修改已有函数或组件

```bash
codegraph explore "目标符号 相关流程"
codegraph impact 目标符号
```

修改后：

```bash
codegraph sync
```

再选择测试：

```bash
codegraph affected changed/file/path
```

### 11.3 修 bug

先查入口和调用链：

```bash
codegraph explore "错误现象 涉及模块 关键符号"
```

如果找到疑似函数：

```bash
codegraph callers 疑似函数
codegraph callees 疑似函数
```

修复后：

```bash
codegraph sync
codegraph affected 修过的文件
```

### 11.4 做代码审查

对变更文件先看受影响测试：

```bash
git diff --name-only | codegraph affected --stdin
```

对核心符号看影响范围：

```bash
codegraph impact 核心符号 --depth 3
```

对不熟悉区域用：

```bash
codegraph explore "变更涉及的模块和符号"
```

## 12. 常见问题处理

### 12.1 提示未初始化

现象：`CodeGraph not initialized` 或找不到 `.codegraph/`。

处理：

```bash
codegraph init
codegraph status
```

### 12.2 修改后查询结果像旧代码

处理：

```bash
codegraph sync
```

如果仍异常：

```bash
codegraph index
```

### 12.3 索引被 lock 卡住

先确认没有正在运行的索引任务，再执行：

```bash
codegraph unlock
codegraph sync
```

### 12.4 索引很慢

处理顺序：

1. 检查是否把依赖、构建产物、大型 vendor 目录纳入索引。
2. 用 `.gitignore` 或 `codegraph.json` 排除不必要目录。
3. 用 `codegraph index --verbose` 查看详细索引过程。

### 12.5 文件或符号缺失

检查：

- 文件语言是否受支持。
- 文件是否在 `.gitignore` 或默认排除目录内。
- 是否需要 `codegraph.json` 的 `include` 或 `extensions`。
- 是否需要 `codegraph sync` 或 `codegraph index`。

## 13. 本项目中的使用约定

在本项目 `coding-agent` 中，仓库根目录存在 `.codegraph/` 时，Agent 应按以下规则执行：

1. 代码理解、定位、影响分析优先使用 `codegraph explore`。
2. 已知符号精读使用 `codegraph node`。
3. 修改共享 hook、store、service、runtime、tool、API 契约前，先用 `codegraph impact` 或 `codegraph explore` 看影响范围。
4. 修改代码后，如果还要继续依赖 CodeGraph，先执行 `codegraph sync`。
5. 选择测试范围时可用 `codegraph affected`，但客户端/后端关键路径仍需跑项目规定的测试和构建。
6. 不要把 `.codegraph/` 当作提交内容或业务代码修改对象。

## 14. 命令选择建议

如果 Agent 不确定该用哪个命令，按这个顺序选择：

1. 不知道入口：`codegraph explore "自然语言问题 + 关键模块名"`
2. 知道符号名：`codegraph node 符号名`
3. 只知道模糊名称：`codegraph query 名称`
4. 准备改公共符号：`codegraph impact 符号名`
5. 想看谁调用它：`codegraph callers 符号名`
6. 想看它调用了谁：`codegraph callees 符号名`
7. 修改后继续查：`codegraph sync`
8. 想决定跑哪些测试：`codegraph affected 文件路径`
9. 索引坏了：`codegraph status` → `codegraph sync` → `codegraph index`

## 15. 参考来源

- CodeGraph GitHub 仓库：https://github.com/colbymchenry/codegraph
- CodeGraph README 安装与 CLI 说明：https://raw.githubusercontent.com/colbymchenry/codegraph/main/README.md
- 本机 `codegraph --help` 与各子命令 `--help` 输出。
