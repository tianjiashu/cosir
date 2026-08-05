/**
 * 变更集批量操作工具栏组件。
 *
 * 仅承载批量操作：
 * - 左侧展示「已选 N 项」
 * - 右侧「保留选中」「撤销选中」「刷新」按钮
 * - ``selectedCount === 0`` 时「保留选中」「撤销选中」禁用
 *
 * 检查点选择由独立的 ``ChangeCheckpointSelect`` 承担，二者在 ``ChangesTab`` 中并列。
 *
 * @module components/right-panel/ChangesToolbar
 */

import { Check, RotateCcw, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

/** ChangesToolbar 组件属性。 */
interface ChangesToolbarProps {
  /** 当前选中的文件数。 */
  selectedCount: number;
  /** 保留选中文件。 */
  onKeepSelected: () => void;
  /** 撤销选中文件。 */
  onRevertSelected: () => void;
  /** 全量刷新。 */
  onRefresh: () => void;
}

/**
 * 变更集批量操作工具栏。
 *
 * @param props - 组件属性。
 * @returns 变更集批量操作工具栏。
 */
export function ChangesToolbar({
  selectedCount,
  onKeepSelected,
  onRevertSelected,
  onRefresh,
}: ChangesToolbarProps) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="shrink-0 text-xs text-muted-foreground">已选 {selectedCount} 项</span>
      <div className="flex flex-wrap gap-1">
        <Button
          variant="outline"
          size="sm"
          className="h-7 gap-1 text-xs"
          disabled={selectedCount === 0}
          onClick={onKeepSelected}
        >
          <Check className="h-3.5 w-3.5" />
          保留选中
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-7 gap-1 text-xs"
          disabled={selectedCount === 0}
          onClick={onRevertSelected}
        >
          <RotateCcw className="h-3.5 w-3.5" />
          撤销选中
        </Button>
        <Button variant="outline" size="sm" className="h-7 gap-1 text-xs" onClick={onRefresh}>
          <RefreshCw className="h-3.5 w-3.5" />
          刷新
        </Button>
      </div>
    </div>
  );
}
