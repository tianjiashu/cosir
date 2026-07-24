/**
 * 思考过程折叠展示组件。
 *
 * 模仿 Codex 风格：默认折叠显示「深度思考 >」，点击展开后显示完整思考内容
 * 并切换为「深度思考 ∨」。使用 lucide-react 的 ChevronRight 图标指示展开状态。
 *
 * @module components/chat/ThinkingBlock
 */

import { useState } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

/** ThinkingBlock 组件属性。 */
interface ThinkingBlockProps {
  /** 思考过程的完整文本内容。 */
  content: string;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * 可折叠的思考过程展示组件。
 *
 * 默认折叠，点击标题栏切换展开/收起；展开时以浅色背景区域展示
 * 原始文本（保留换行与空格），与参考截图的「深度思考」交互一致。
 */
export function ThinkingBlock({ content, className }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={cn("w-full", className)}>
      {/* 折叠/展开标题栏 */}
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex cursor-pointer items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronRight
          className={cn(
            "h-4 w-4 transition-transform duration-200",
            expanded && "rotate-90",
          )}
        />
        <span>深度思考</span>
      </button>

      {/* 展开内容区 */}
      {expanded ? (
        <div className="mt-2 rounded-lg border border-muted bg-muted/50 px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
          <div className="whitespace-pre-wrap">{content}</div>
        </div>
      ) : null}
    </div>
  );
}
