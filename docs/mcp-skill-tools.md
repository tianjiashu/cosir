# MCP 与 Skill 工具（首期）

首期在 Agent 工具目录中注册两个固定入口：`mcp` 和 `skill`。系统配置中心提供 MCP 服务与 Skills 两个独立页面；Agent 运行时仍通过固定工具入口发现并调用能力。两个入口均提供 `list` 动作，避免 Agent 在没有动态工具 schema 时猜测服务名、工具名和 Skill 名。

## MCP 服务

配置文件位于系统级 `<DATA_DIR>/.cosir/config/mcp_servers.json`。每个服务可选择 stdio、SSE 或 Streamable HTTP。

stdio 服务使用 `command`、`args` 和 `env` 启动本机进程：

```json
{
  "servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/workspace"],
      "env": {},
      "enabled": true
    }
  ]
}
```

HTTP 服务使用 `url` 连接 endpoint，`headers` 可提供该服务需要的请求头。SSE 和 Streamable HTTP 使用相同的 HTTP 配置字段：

```json
{
  "servers": [
    {
      "name": "remote-tools",
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer token"},
      "enabled": true
    },
    {
      "name": "legacy-events",
      "transport": "sse",
      "url": "https://example.com/sse",
      "headers": {},
      "enabled": true
    }
  ]
}
```

`action: "list"` 连接已启用的服务并返回每项工具的名称、描述和输入 schema。`action: "call"` 使用 `server_name`、`tool_name` 和 `arguments` 调用其中一项。每次发现或调用都会建立并关闭一次客户端会话；调用在隔离工具子进程内运行，超时由现有工具执行器负责清理进程树。

系统配置页面支持读取和保存服务列表、启停服务、测试未保存的连接配置并查看发现到的工具。测试结果仅表示本次连接，不代表服务常驻在线。

## Skill

Skill 统一放在 `<DATA_DIR>/.cosir/skills/<skill-name>/`。每个目录至少包含带 YAML frontmatter 的 `SKILL.md`，配套文本资源与其放在同一目录：

```markdown
---
name: example-skill
description: 说明这个 Skill 做什么，以及何时使用。
---

Skill 的任务步骤和约束写在这里。
```

`<DATA_DIR>/.cosir/config/skills.json` 用 `enabled_skills` 管理允许 Agent 使用的名称：

```json
{
  "enabled_skills": ["example-skill"]
}
```

配置文件不存在时默认启用所有有效 Skill；文件存在时，只启用列表中的 Skill。`action: "list"` 仅返回启用项；`action: "load"` 读取启用 Skill 的 `SKILL.md` 正文，或读取 `resource_path` 指定的目录内文本资源。工具只读取文本，不执行 Skill 内脚本。

MCP 与 Skill 配置在工具调用时读取，保存后新调用即可使用更新后的启停状态；两个固定工具入口本身不随配置变化。

Skills 页面列出系统目录中的有效 Skill，允许启用、停用、查看只读正文和删除。导入从本机选择单个 Skill 文件夹，并复制到系统目录；目录内的 Skill 名称必须与 `SKILL.md` 元数据一致，同名导入返回冲突，不覆盖已有内容。
