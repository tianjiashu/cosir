/**
 * Checkpoint 区块占位组件。
 *
 * 预留右侧面板中 checkpoint 独立展示区域，
 * 第一版仅显示占位提示。
 *
 * @module components/right-panel/CheckpointBlock
 */

import { Bookmark } from "lucide-react";

/**
 * CheckpointBlock Checkpoint 占位区块。
 *
 * 后续接入：展示任务运行过程中的状态快照列表，
 * 允许用户查看和恢复到特定 checkpoint。
 */
export function CheckpointBlock() {
  return (
    <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 opacity-60">
      <span className="text-muted-foreground">
        <Bookmark className="h-4 w-4" />
      </span>
      <div>
        <p className="text-xs font-medium">Checkpoint</p>
        <p className="text-xs text-muted-foreground">任务状态快照（即将上线）</p>
      </div>
    </div>
  );
}
