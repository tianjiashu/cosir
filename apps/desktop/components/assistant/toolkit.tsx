"use client";

import { defineToolkit } from "@assistant-ui/react";
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui";

/**
 * 后端工具的 UI-only registry。
 *
 * 工具执行、权限和结果事实全部由后端负责；这里仅把已知工具绑定到
 * Assistant UI renderer。未注册工具继续由 Thread 的 ToolFallback 兜底。
 */
export const codingAgentToolkit = defineToolkit({
  list_directory: { type: "backend", render: ToolFallback },
  read_file: { type: "backend", render: ToolFallback },
  search_files: { type: "backend", render: ToolFallback },
  write_file: { type: "backend", render: ToolFallback },
  patch: { type: "backend", render: ToolFallback },
  apply_patch: { type: "backend", render: ToolFallback },
  delete: { type: "backend", render: ToolFallback },
  execute_terminal: { type: "backend", render: ToolFallback },
  web_search: { type: "backend", render: ToolFallback },
  web_extract: { type: "backend", render: ToolFallback },
  delegate_task: { type: "backend", render: ToolFallback },
  codegraph_explore: { type: "backend", render: ToolFallback },
  codegraph_search: { type: "backend", render: ToolFallback },
  codegraph_node: { type: "backend", render: ToolFallback },
  codegraph_callers: { type: "backend", render: ToolFallback },
  codegraph_callees: { type: "backend", render: ToolFallback },
  codegraph_impact: { type: "backend", render: ToolFallback },
});
