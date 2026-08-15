/**
 * 变更集批量操作工具栏组件。
 *
 * 仅承载两个批量动作：
 * - 「保留」：把所有 pending 文件标记为已保留，前端过滤 pending 后列表自动清空。
 * - 「撤销」：把所有 pending 文件还原到变更前，列表自动清空。
 *
 * 文件列表为空时两按钮禁用，避免空操作。
 *
 * @module components/right-panel/ChangesToolbar
 */

import { Check, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

/** ChangesToolbar 组件属性。 */
interface ChangesToolbarProps {
  /** 是否有可操作的 pending 文件（为空时禁用两按钮）。 */
  hasFiles: boolean;
  /** 保留全部 pending 文件。 */
  onKeepAll: () => void;
  /** 撤销全部 pending 文件。 */
  onRevertAll: () => void;
}

/**
 * 变更集批量操作工具栏。
 *
 * @param props - 组件属性。
 * @returns 变更集批量操作工具栏。
 */
export function ChangesToolbar({ hasFiles, onKeepAll, onRevertAll }: ChangesToolbarProps) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <Button
        variant="primary"
        size="sm"
        className="h-7 gap-1 text-xs"
        disabled={!hasFiles}
        onClick={onKeepAll}
      >
        <Check className="h-3.5 w-3.5" />
        保留
      </Button>
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1 text-xs"
        disabled={!hasFiles}
        onClick={onRevertAll}
      >
        <RotateCcw className="h-3.5 w-3.5" />
        撤销
      </Button>
    </div>
  );
}