/**
 * 变更集单行文件条目组件。
 *
 * 单行紧凑布局：
 * - 左：文件路径（truncate + title 显示全路径）
 * - 中：``+N`` / ``-M`` diff 增删徽标（按存在性条件渲染，默认态显示，不展示 action 标签）
 * - 右：hover / 键盘聚焦时才淡入的「保留（✓）」「撤销（✗）」图标按钮组
 *
 * 交互设计：
 * - 默认态只显示 ``+N`` / ``-M`` diff 徽标（仅渲染实际存在的一侧，两侧都有时以 ``-`` 拼接为
 *   ``+N -M``），行内无常驻按钮，保持单行紧凑（``py-1.5``），
 *   可视区能容纳更多变更行。
 * - 鼠标 hover 行或键盘聚焦行内按钮时：diff 徽标隐藏（``group-hover/row:hidden``），
 *   右侧「保留」「撤销」图标按钮组淡入（``opacity-0`` →
 *   ``group-hover/row:opacity-100`` / ``focus-within:opacity-100``），保证鼠标路径与
 *   键盘 Tab 路径均可达。
 * - 「保留（✓）」：调 ``onKeep`` 标记该文件变更为已保留，前端过滤 pending 后该文件
 *   自动从列表消失；「撤销（✗）」：调 ``onRevert`` 把文件还原到变更前。
 * - diff 徽标仅在 ``additions`` / ``deletions`` 至少其一大于 0 时渲染，避免伪造 0；每侧
 *   各自条件化渲染（``+N`` 仅在 ``additions > 0``，``-M`` 仅在 ``deletions > 0``）。
 *
 * 注意：本组件只展示 status === 'pending' 的文件（由 ChangesPanel 过滤），故无需处理
 * kept / reverted 的视觉态——保留 / 撤销动作完成后该行会从列表移除。
 *
 * @module components/right-panel/ChangeFileRow
 */

import { memo } from "react";
import { Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ChangeFile } from "@shared/api";

/** ChangeFileRow 组件属性。 */
interface ChangeFileRowProps {
  /** 文件变更条目。 */
  file: ChangeFile;
  /** 保留单个文件（将该条目标记为已保留并从 pending 列表移除）。 */
  onKeep: (path: string) => void;
  /** 撤销单个文件（还原磁盘 + 从 pending 列表移除）。 */
  onRevert: (path: string) => void;
}

/**
 * 变更集单行文件条目。
 *
 * 用 `memo` 包裹：当 `file` / 回调引用不变时跳过重渲染，
 * 配合 `ChangesPanel` 的虚拟列表渲染，避免大变更集整列表重渲染。
 * diff 徽标与操作按钮的显隐切换由 CSS（``group-hover/row`` + ``focus-within``）控制，
 * 不触发 React 重渲染。
 *
 * @param props - 组件属性。
 * @returns 单行变更条目。
 */
export const ChangeFileRow = memo(function ChangeFileRow({
  file,
  onKeep,
  onRevert,
}: ChangeFileRowProps) {
  const hasDiff = file.additions > 0 || file.deletions > 0;

  return (
    <div className="group/row flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent/50 focus-within:bg-accent/50">
      <p className="min-w-0 flex-1 truncate font-mono text-xs" title={file.path}>
        {file.path}
      </p>
      {hasDiff && (
        <span className="shrink-0 whitespace-nowrap font-mono text-xs leading-none group-hover/row:hidden">
          {file.additions > 0 && (
            <span className="text-emerald-600">+{file.additions}</span>
          )}
          {file.additions > 0 && file.deletions > 0 && (
            <span className="mx-1 text-muted-foreground">-</span>
          )}
          {file.deletions > 0 && (
            <span className="text-red-500">-{file.deletions}</span>
          )}
        </span>
      )}
      <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover/row:opacity-100 focus-within:opacity-100">
        <Button
          variant="outline"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={() => onKeep(file.path)}
          aria-label={`保留 ${file.path}`}
          title="保留"
        >
          <Check className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={() => onRevert(file.path)}
          aria-label={`撤销 ${file.path}`}
          title="撤销"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
});