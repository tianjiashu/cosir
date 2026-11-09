# MCP 与 Skill 工具

Agent 工具目录里有两个固定入口：`mcp` 和 `skill`。两者都提供 `list` 动作，Agent 不需要猜测服务名、工具名或 Skill 名。工具的参数 schema 不随配置变化，所以 Task 固化的 schema 和提示词缓存不受配置变更影响。

## 作用域

Skill 与 MCP 各分两个作用域，目录布局完全一致：

| | 系统级（所有工作区共用） | workspace 级（只在该工作区可用） |
|---|---|---|
| Skill 目录 | `<DATA_DIR>/.cosir/skills/<name>/SKILL.md` | `<workspace>/.cosir/skills/<name>/SKILL.md` |
| Skill 启用名单 | `<DATA_DIR>/.cosir/config/skills.json` | `<workspace>/.cosir/config/skills.json` |
| MCP 服务配置 | `<DATA_DIR>/.cosir/config/mcp_servers.json` | `<workspace>/.cosir/config/mcp_servers.json` |
| MCP 本机测试记录 | `<DATA_DIR>/.cosir/config/mcp_tested_servers.json`（两个作用域共用这一份） | — |

规则：

- Agent 看到两层合并的结果，同名时 workspace 覆盖系统级。删掉 workspace 的覆盖项后，系统级同名项重新可见。
- 每个作用域只管自己的条目：在 workspace 配置页能看到继承来的系统级条目，但只读，不能启停或删除。
- 配置每次调用时现读文件，不缓存：手工放进目录、或随 git 拉下来的 Skill 立刻可见。
- 作用域、路径和分层顺序统一由 `app/service/configuration/extension_scope.py` 提供。

## MCP 服务

每个服务可选 stdio、SSE 或 Streamable HTTP 三种传输。stdio 服务用 `command`、`args`、`env` 启动本机进程：

```json
{
  "servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {},
      "enabled": true
    }
  ]
}
```

HTTP 服务用 `url` 连接 endpoint，用 `headers` 提供该服务需要的请求头。SSE 与 Streamable HTTP 使用相同的字段。

**测试通过才可用**

一个服务要同时满足三个条件，Agent 才能用它：

1. 已启用；
2. 当前连接配置在本机测试通过；
3. 测试记录的连接指纹与当前配置一致。

测试记录只存在系统级，按「作用域 → 服务名 → 测试通过的连接指纹」保存。workspace 的 `mcp_servers.json` 可能随仓库共享，仓库里预先写好的「已测试」不能让未经本机测试的命令直接交给 Agent。所以该文件只接受 `servers` 字段，夹带其他字段按格式无效处理。

测试的可以是还没保存的草稿：测试成功即记录，之后保存同一配置就能立即使用；测试草稿（无论成败）不影响已保存配置的测试结果。以下情况需要重新测试：

- 保存时，本作用域只保留「仍存在的服务的当前配置」的记录。删掉的服务再加回来、改过连接参数，都要重测；
- 删除工作区时，它的记录一并丢弃；
- 对已保存的配置重测失败，会撤销它的可用状态。

任一层的 `mcp_servers.json` 格式无效时，这个工作区的 MCP 整体不可用（失败即关闭）：Agent 调用 `mcp` 会得到配置错误，修好文件即恢复。

**连接与运行**

- `action: "list"` 列出当前工作区可用服务的工具（名称、描述、输入 schema），`action: "call"` 调用其中一项。
- 连接由进程级 `MCPConnectionPool` 常驻持有，按「作用域 + 服务名」区分。两个工作区的同名服务各用各的连接，互不顶替。
- workspace 的 stdio 服务以工作区根目录为 cwd，系统级服务沿用后端进程目录。
- stderr 日志写在 `.cosir/logs/mcp/`。workspace 服务的日志文件名带工作区路径的短哈希。

## Skill

每个 Skill 目录至少包含一个带 YAML frontmatter 的 `SKILL.md`，`name` 必须与目录名一致：

```markdown
---
name: example-skill
description: 说明这个 Skill 做什么，以及何时使用。
---

Skill 的任务步骤和约束写在这里。
```

**启用名单**

启用名单的格式是 `{"enabled_skills": [...]}`，只管本作用域自己目录里的 Skill。文件不存在时，本作用域的 Skill 全部启用。

**使用方式**

Agent 有两条路使用 Skill，拿到的正文一致：

- **Agent 自己加载**：`action: "load"` 读取已启用 Skill 的正文，正文前附 Skill 目录的绝对路径，正文里的 `scripts/`、`references/` 等相对路径以它为基准。带 `resource_path` 时读取目录内的一个文本文件。工具只读取文本，不执行脚本，脚本由终端工具运行。
- **用户在输入框选中**：正文里写入 token `[[cosir-skill:<name>]]`。后端在入队时校验这个 Skill（不存在或已停用时以 400 `SKILL_UNAVAILABLE` 拒绝），把它的正文与目录固化进这条输入的 `<skill>` 块，模型不需要再调用工具。展示时 token 留在文本里，由前端渲染成胶囊。手打的其他写法只是普通文字。

**配置页**

配置页可以导入 Skill 文件夹或 ZIP（复制到本作用域目录）、启停、查看只读正文、删除。同一作用域内同名导入返回冲突，不会覆盖已有内容。workspace 导入与系统级同名的 Skill 属于有意覆盖。

## 目录变更通知

Task 首次装配时，子 Agent、Agent Team、Skill、MCP 四种目录写进系统提示词并固化。之后通过配置页做的写入，会给已在本进程装配过、固化了对应工具的 Task 投递一条 `catalog_change` 通知，实现见 `runtime_notices/catalog_change.py`。

**命中范围**

- 系统级变更命中所有工作区，但跳过有同名覆盖项的工作区；
- workspace 变更只命中该工作区。

**不通知的情况**

- 可见目录没有变化（例如停用一个本来就停用的 Skill）；
- 手工改文件或 git 拉取带来的变化（`list` 动作每次现读，可以兜底）。
