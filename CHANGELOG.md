# Changelog

## 文件工具集落地（2026-07-24）

新增 6 个生产级文件工具：`write_file` / `edit_file` / `apply_patch` / `grep` /
`search_files` / `list_directory`，并重构 `read_file` 复用共享的 `ProjectPathResolver`
路径安全抽象。工具经自定义 `ToolDefinition` 契约接入 `ToolScheduler`，不绑定 LangGraph。

### 已知临时缺口（待审批 UI 落地后移除）

- `file_write` / `file_search` 当前经 `ToolScheduler.allowed_permissions` 白名单**默认放行**，
  属临时状态。审批 UI（`interrupt()`）未落地前，写/搜能力实际**无门禁**。
- 此缺口在 `apps/backend/app/tools/tool_system.py` 的 `build_tool_system` 注释中已记录，
  此处同步以 changelog 形式标注，满足技术方案第 7/11 节「代码与 changelog 明确标注」要求。
- 后续接入审批中断（`interrupt()`）+ 审批 UI 后，应从白名单移除 `file_write` / `file_search`，
  改为按 `risk_level` 触发审批。
