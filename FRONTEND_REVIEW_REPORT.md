# apps/desktop 前端审查报告

审查日期：2026-10-14  
审查范围：`apps/desktop` 当前工作树（包含既有未提交改动）；为确认跨层契约，少量只读检查了 Skill 配置 API/service。未修改业务代码。

## 结论摘要

发现 2 项可复现的逻辑/安全缺陷，另有 2 项性能与结构维护建议。没有发现已证实的 React HTML 注入问题：composer 的动态文本在构造 HTML 前做了转义；对话 Markdown 通过 Streamdown，并启用了链接安全与协议限制。构建和所选单测通过并不覆盖下述两个缺陷。

## 已确认发现

### P2 — 工具 URL 过滤可被 IPv4-mapped IPv6 绕过，并触发自动本机请求

**位置**

- `apps/desktop/components/assistant-ui/tools/types.ts:49-70`
- `apps/desktop/components/assistant-ui/tools/details-tool.tsx:194-206`

**触发与影响**

`safeExternalUrl` 只过滤点分 IPv4 私网地址、`::1` 和少数 IPv6 前缀。输入 `https://[::ffff:127.0.0.1]/` 经 WHATWG URL 解析后，hostname 变为 `[::ffff:7f00:1]`；去掉方括号后仍不满足现有拒绝条件，因而通过过滤。URL 结果 renderer 随即把 `<origin>/favicon.ico` 放进 `img.src`，查看工具结果就会自动发出回环地址请求。

这是工具结果到 WebView 的网络访问边界缺口，可被不可信的搜索结果/工具结果用于探测或触发本机 HTTP 服务请求。当前用例通过仅说明普通私网地址被拦截，不覆盖该变体。

**交叉验收**

子代理独立确认过滤条件遗漏该地址，且自动 favicon 请求路径成立。主审使用 Node WHATWG URL 独立确认 hostname 规范化结果为 `[::ffff:7f00:1]`。现有 `tests/unit/tool-part.test.ts` 的 13 项通过，但只覆盖普通 `127.0.0.1`、`192.168.1.20` 等，未覆盖 mapped IPv6。

**建议**

用完整的 IPv4/IPv6 地址解析来判定回环与私网地址；同时考虑取消对不可信 URL 的自动 favicon 请求，改为静态图标或在用户明确操作后再加载。

### P2 — 快速切换不同 Skill 会丢失一次启停操作

**位置**

- `apps/desktop/components/configuration/skill-configuration-panel.tsx:63-76,190-191`
- `apps/desktop/lib/api/system-extensions.ts:77-81`
- `apps/backend/app/service/configuration/skill_configuration_service.py:66-81`

**触发与影响**

每次切换都根据闭包中的整份 `skills` 列表生成完整启用名单并发 PUT，但 UI 只禁用当前 `busySkill` 对应的行。初始 A、B 均启用时，快速先停用 A、再停用 B，两次请求可能分别提交 `[B]` 和 `[A]`。后端将请求中的名单整体写入配置；后写入者覆盖先写入者，最终只停用其中一个 Skill，另一项用户操作丢失。即使请求串行处理，第二份陈旧名单仍会覆盖第一份结果。

**交叉验收**

子代理核对了前端 PUT、API 路由和 service 的全量写入行为，确认没有后端合并或前端全局串行机制消除此问题。

**建议**

在该全量替换请求完成前禁用所有 Skill 状态变更，或在前端将变更串行化并从最新状态生成名单。单纯禁用当前行不足以避免跨行竞争。

## 性能与维护性建议

### P3 — 首屏静态包包含很大的 Markdown 依赖 chunk，且 chunk 之间有循环依赖

**位置**

- `apps/desktop/vite.config.ts:36-66`（手动 chunk 归类）
- `apps/desktop/components/markdown-rendering-config.ts:1-16`（静态导入 Markdown 插件）

**证据与影响**

本次 `npm run build` 成功，但 Vite 报告 `vendor-markdown` 与 `vendor-shared` 存在循环 chunk 依赖；产物分别约为 12.7 MB / 2.51 MB gzip 和 2.55 MB / 806 KB gzip。当前主入口静态引用 Mermaid、Shiki 等 Markdown 能力，增加桌面端首次加载与内存成本。没有证据表明它造成当前功能故障，故列为性能建议而非缺陷。

**建议**

用构建分析确认占比后，评估将 Mermaid、代码高亮等低频能力按需加载，并调整有环的手动分包边界。

### P3 — Composer 编辑器文件职责过重

**位置**

- `apps/desktop/components/composer/inline-attachment-input.tsx`（864 行）

该文件同时负责插入桥接 context、附件与 Skill token HTML 渲染/序列化、逻辑光标计算、contenteditable 浏览器事件、附件移除和诊断日志。当前 Skill token 扩展继续扩大了附件输入组件的职责面，后续改动更容易影响光标与附件行为，也较难针对 token 类型隔离验证。

**建议**

后续结构整理可按职责拆分 token 编解码/渲染、插入协调和编辑器生命周期逻辑；保持当前任务范围时无需为拆分而拆分。

## 验收记录与范围

- `npx vitest run tests/unit/mcp-config-json.test.ts tests/unit/prepare-user-command.test.ts tests/unit/converter.test.ts tests/unit/inline-attachment-input.test.ts`：4 个文件、44 项通过。
- 子代理执行 `npx.cmd vitest run tests/unit/tool-part.test.ts`：1 个文件、13 项通过。
- `npm run build`：TypeScript 检查与 Vite production build 成功；出现上述 chunk 循环/体积警告。
- 未运行全量 E2E 或全部单测；审查是源码审查，不等同于完整安全测试。工作区其他未提交改动保持原样。
