/**
 * Subagent 分组占位组件。
 *
 * 预留右侧面板中子 Agent 分组展示区域，
 * 第一版仅显示占位提示。
 *
 * @module components/right-panel/SubagentBlock
 */

import { Users } from "lucide-react";

/**
 * SubagentBlock Subagent 分组占位区块。
 *
 * 后续接入：展示任务执行过程中启动的子 Agent 列表，
 * 包括各子 Agent 的状态、输出和工具调用记录。
 */
export function SubagentBlock() {
  return (
    <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 opacity-60">
      <span className="text-muted-foreground">
        <Users className="h-4 w-4" />
      </span>
      <div>
        <p className="text-xs font-medium">Subagent</p>
        <p className="text-xs text-muted-foreground">子 Agent 分组（即将上线）</p>
      </div>
    </div>
  );
}
