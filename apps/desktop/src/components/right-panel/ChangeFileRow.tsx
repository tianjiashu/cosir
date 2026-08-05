/**
 * 变更集单行文件条目组件。
 *
 * 单行三段式布局：
 * - 左：checkbox（多选）+ 文件路径（truncate + title 显示全路径）
 * - 中：action 标签（created→新建 / modified→修改 / deleted→删除）
 * - 右：「保留」「撤销」两个操作按钮
 *
 * 状态样式：``reverted`` 整行降透明度、两按钮禁用；``kept``「保留」按钮呈已选中态。
 *
 * @module components/right-panel/ChangeFileRow
 */

import { memo } from "react";
import { Check, RotateCcw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ChangeFile } from "@shared/api";

/** 变更动作 → 展示标签与 Badge 变体。 */
const ACTION_META: Record<string, { label: string; variant: "default" | "secondary" | "success" }> = {
  created: { label: "新建", variant: "success" },
  modified: { label: "修改", variant: "default" },
  deleted: { label: "删除", variant: "secondary" },
};

/** ChangeFileRow 组件属性。 */
interface ChangeFileRowProps {
  /** 文件变更条目。 */
  file: ChangeFile;
  /** 是否被多选。 */
  selected: boolean;
  /** 切换多选状态。 */
  onToggleSelect: (path: string) => void;
  /** 保留单个文件。 */
  onKeep: (path: string) => void;
  /** 撤销单个文件。 */
  onRevert: (path: string) => void;
}

/**
 * 变更集单行文件条目。
 *
 * 用 `memo` 包裹：当 `file` / `selected` / 回调引用不变时跳过重渲染，
 * 配合 `ChangesTab` 的虚拟列表渲染，避免大变更集整列表重渲染。
 *
 * @param props - 组件属性。
 * @returns 单行变更条目。
 */
export const ChangeFileRow = memo(function ChangeFileRow({
  file,
  selected,
  onToggleSelect,
  onKeep,
  onRevert,
}: ChangeFileRowProps) {
  const actionMeta = ACTION_META[file.action] ?? { label: file.action, variant: "default" as const };
  const isReverted = file.status === "reverted";
  const isKept = file.status === "kept";

  return (
    <div
      className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors ${
        isReverted ? "opacity-50" : "hover:bg-accent/50"
      }`}
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={() => onToggleSelect(file.path)}
        disabled={isReverted}
        aria-label={`选择 ${file.path}`}
        className="h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed"
      />
      <p className="min-w-0 flex-1 truncate font-mono text-xs" title={file.path}>
        {file.path}
      </p>
      <Badge variant={actionMeta.variant} className="shrink-0">
        {actionMeta.label}
      </Badge>
      <div className="flex shrink-0 gap-1">
        <Button
          variant={isKept ? "default" : "outline"}
          size="sm"
          className="h-7 gap-1 text-xs"
          disabled={isKept || isReverted}
          onClick={() => onKeep(file.path)}
        >
          <Check className="h-3.5 w-3.5" />
          保留
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-7 gap-1 text-xs"
          disabled={isReverted}
          onClick={() => onRevert(file.path)}
        >
          <RotateCcw className="h-3.5 w-3.5" />
          撤销
        </Button>
      </div>
    </div>
  );
});
