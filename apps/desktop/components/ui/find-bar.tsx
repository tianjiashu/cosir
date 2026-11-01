import { ChevronDownIcon, ChevronUpIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { cn } from "@/lib/utils";

/**
 * 应用内唯一的查找条（终端、文件源码、Markdown 渲染、PDF 共用）：边输入边查，Enter 下一处、Shift+Enter
 * 上一处、Esc 关闭。只管输入与展示；在哪里查、怎么高亮由调用方在 `onSearch` 里决定，并把位置经 `result`
 * 交回。浮在内容右上角，由调用方用 `className` 定位。`focusKey` 变化时重新聚焦并全选（再按一次 Ctrl/Cmd+F）。
 */
export function FindBar({ label, result, onSearch, onClose, focusKey, className, children }: {
  label: string;
  result: FindResult | null;
  onSearch: (term: string, direction: FindDirection) => void;
  onClose: () => void;
  focusKey?: number;
  className?: string;
  /** 附加操作（如分段文件的「在整个文件中查找」），排在翻页按钮之后。 */
  children?: ReactNode;
}) {
  const [term, setTerm] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusKey]);
  const summary = !term ? "" : result === null || result.count === 0 ? "无结果" : result.index < 0 ? `${result.count}+` : `${result.index + 1}/${result.count}`;
  return (
    <div
      role="search"
      className={cn("bg-popover text-popover-foreground flex items-center gap-1 rounded-md border p-1 shadow-md", className)}
    >
      <input
        ref={inputRef}
        value={term}
        aria-label={label}
        placeholder="查找"
        className="h-6 w-44 min-w-0 bg-transparent px-1.5 text-xs outline-none"
        onChange={(event) => {
          setTerm(event.target.value);
          onSearch(event.target.value, "next");
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onSearch(term, event.shiftKey ? "previous" : "next");
          } else if (event.key === "Escape") {
            event.preventDefault();
            onClose();
          }
        }}
      />
      <span className="text-muted-foreground min-w-10 text-right text-2xs tabular-nums" aria-live="polite">{summary}</span>
      <TooltipIconButton tooltip="上一个" aria-label="上一个匹配" className="size-6" onClick={() => onSearch(term, "previous")}>
        <ChevronUpIcon aria-hidden="true" />
      </TooltipIconButton>
      <TooltipIconButton tooltip="下一个" aria-label="下一个匹配" className="size-6" onClick={() => onSearch(term, "next")}>
        <ChevronDownIcon aria-hidden="true" />
      </TooltipIconButton>
      {children}
      <TooltipIconButton tooltip="关闭搜索" aria-label="关闭搜索" className="size-6" onClick={onClose}>
        <XIcon aria-hidden="true" />
      </TooltipIconButton>
    </div>
  );
}
