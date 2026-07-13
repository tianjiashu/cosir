/**
 * MCP 入口占位组件。
 *
 * 预留右侧面板中 Model Context Protocol（MCP）入口区域，
 * 第一版仅显示占位提示。
 *
 * @module components/right-panel/McpBlock
 */

import { Puzzle } from "lucide-react";

/**
 * McpBlock MCP 入口占位区块。
 *
 * 后续接入：展示已连接的 MCP 服务器列表，
 * 以及各服务器提供的工具和能力。
 */
export function McpBlock() {
  return (
    <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 opacity-60">
      <span className="text-muted-foreground">
        <Puzzle className="h-4 w-4" />
      </span>
      <div>
        <p className="text-xs font-medium">MCP</p>
        <p className="text-xs text-muted-foreground">模型上下文协议入口（即将上线）</p>
      </div>
    </div>
  );
}
