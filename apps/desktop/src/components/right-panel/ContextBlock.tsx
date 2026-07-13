/**
 * 上下文引用 / Context Compaction 区块占位组件。
 *
 * 预留右侧面板中上下文引用和 Compaction 指标展示区域，
 * 第一版仅显示占位提示。
 *
 * @module components/right-panel/ContextBlock
 */

import { Database } from "lucide-react";

/**
 * ContextBlock 上下文引用占位区块。
 *
 * 后续接入：展示当前任务的上下文使用量、
 * Compaction（压缩）次数和指标。
 */
export function ContextBlock() {
  return (
    <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 opacity-60">
      <span className="text-muted-foreground">
        <Database className="h-4 w-4" />
      </span>
      <div>
        <p className="text-xs font-medium">上下文引用</p>
        <p className="text-xs text-muted-foreground">Context Compaction 指标（即将上线）</p>
      </div>
    </div>
  );
}
