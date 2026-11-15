# Skill 与 MCP：与成熟设计的差距

调研日期：2026-11-15。对照对象：Claude Code、Codex、Agent Skills 标准、MCP 规范（2025-06 / 2025-11 版）。外部规范按调研时已知情况对照，未联网复核最新变化。本文只记差距与建议，不含实施方案。

## 结论

- **Skill：约八成对齐。** 主干（公共目录、两层合并、渐进披露、配套资源、导入校验）与成熟设计一致；差在进阶字段与目录篇幅控制。
- **MCP：连接层成熟，协议层约三四成。** 长连接池、指纹重建、空闲回收、stderr 落盘、`list_changed` 刷新都扎实；但只实现了「tools + 文本结果」，图片、认证、resources、prompts、elicitation、roots 均缺失。真实可用性的差距主要在这里。

## Skill

### 已对齐

- 目录用生态公共位置 `.agents/skills`，系统级与 workspace 级合并、workspace 同名覆盖（`app/service/configuration/skills/catalog.py`）。
- 渐进披露：系统提示词只列名称与描述，正文与配套资源由 `skill` 工具按需加载（`app/tools/tool_handler/skill_tool.py`）。
- 支持 `disable-model-invocation`（仅手动）；用户在输入框选中的 Skill 以 `<skill>` 胶囊交给模型。
- 校验对外宽松（标准建议）、导入严格；读取受 `ProtectedPaths` 约束；超长正文截断并指明原文位置。

### 差距

| # | 差距 | 成熟做法 | 现状与证据 | 影响 |
|---|---|---|---|---|
| S1 | 目录没有篇幅预算 | Claude Code 对描述目录设字符预算，超出截断或省略 | `render_skill_catalog`（`app/core/context/system_prompt_builder.py:486`）全量注入；配置页只展示估算 token 数，不设上限 | Skill 多了以后每次请求都背着整段目录 |
| S2 | 内置子 Agent 没有 `skill` / `mcp` | Claude Code 子 Agent 默认继承全部工具（含 MCP） | `app/service/configuration/agents/builtin_agents.py:131`、`:171` 的 `allowed_tools` 不含两者（置信度中；用户自定义子 Agent 可自行加） | 委派出去的任务用不上项目 Skill 流程 |
| S3 | 不支持 `allowed-tools`、`model`、`context: fork`、参数替换 | Claude Code 扩展字段：预授权工具、指定模型、在子 Agent 中执行、`$ARGUMENTS` | 均被忽略 | `allowed-tools` 依赖权限体系，随「安全与权限」做；其余为增强 |
| S4 | 已开会话看不到新装 Skill | 成熟产品多数也只在启动时读取 | 系统提示词在 Session 创建时固化；`list` 能现读 | 已记入 AGENTS 待办「环境变化告知」 |
| S5 | 无分发与更新机制 | Claude Code 插件市场、版本 | 只能目录 / zip 导入 | 单用户场景优先级低 |

## MCP

### 已对齐或更好

- 进程级长连接池，按「作用域 + 服务名」持有，连接指纹变化即重建，空闲回收，断线下次重连，失败调用不重放（`app/tools/mcp/connection_pool.py`）。
- 支持 stdio / SSE / Streamable HTTP；stdio 服务 stderr 按服务落盘并轮转。
- workspace 配置须在本机「测试通过」才对 Agent 开放，测试记录不随仓库共享——等价于 Claude Code 对 `.mcp.json` 的首次确认。
- 配置界面兼容常见 `mcpServers` JSON 片段（`apps/desktop/components/configuration/mcp-config-json.ts`）。

### 差距

| # | 差距 | 现状与证据 | 影响 |
|---|---|---|---|
| M1 | **非文本内容全部丢弃** | `_content_text`（`app/tools/mcp/client.py:175`）把图片、音频、资源块换成 `[<type> content returned]` | 截图、设计稿、浏览器类 MCP 基本不可用；项目已有 `tool_image` 链路可复用 |
| M2 | **没有 OAuth** | 远程服务只支持静态 `headers`（`client.py:71`、`:78`） | GitHub、Linear、Notion、Sentry 等托管服务多数要求 OAuth 2.1，目前接不上 |
| M3 | **密钥明文，不支持 `${VAR}` 展开** | `env` / `headers` 原样存入 `mcp_servers.json` | workspace 配置随仓库共享时，要么 Key 进 git，要么无法共享配置 |
| M4 | 单一代理工具 `mcp(list/call)` 缺检索与参数校验 | 参数为 JSON 字符串，调用前不按服务 `inputSchema` 校验；`list` 一次返回全部服务全部工具的完整 schema，不能按关键词筛 | 代理方式为固化 schema、保住缓存，方向成立（与 Claude Code 延迟加载工具同思路）；工具多时 `list` 结果膨胀 |
| M5 | 不支持 roots、elicitation、sampling | 客户端未声明这些能力 | filesystem 类服务依赖 roots 得知工作区；elicitation 可复用现有 HITL `user_decision` |
| M6 | 不支持 resources、prompts | 只调用 tools | 缺「@ 引用资源」「prompt 作斜杠命令」两种入口 |
| M7 | 长时间调用 | 固定 90 秒时限（`app/tools/tool_handler/mcp_tool.py:49`），不处理进度通知，不能转后台 | 长任务到点被放弃，服务端可能仍在执行 |
| M8 | 未使用工具注解（`readOnlyHint` / `destructiveHint`） | 无逐工具审批 | 归入 AGENTS 待办「安全与权限」 |

## 顺带发现：提示词事实过时

`app/core/context/system_prompt_builder.py:169` 告诉模型系统 `.cosir` 中「only its skills/ is readable」。Skill 已迁到 `.agents/skills`，`app/core/permission/protected_paths.py` 对整棵 `.cosir` 禁读写。模型看到的是错误事实，修正只需改一句。

## 建议顺序

1. **M1 + M2 + M3**：决定常见 MCP 服务能否接上，收益最大；M1 复用现有图片链路，改动最小。
2. **S1 + M4 的检索部分**：目录变大后的篇幅控制，Skill 与 MCP 是同一类问题，宜同一思路解决。
3. **M5 的 roots 与 elicitation**：elicitation 复用 HITL。
4. S3、M6、M7 等真实需求出现再做；M8 与 `allowed-tools` 并入「安全与权限」。
