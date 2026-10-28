import { ChevronDownIcon, ChevronUpIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";

type TerminalSearchBarProps = {
  result: { index: number; count: number } | null;
  onSearch: (term: string, direction: "next" | "previous") => void;
  onClose: () => void;
};

/** 终端面板内的搜索框（Ctrl/Cmd+F 打开）：Enter 下一个、Shift+Enter 上一个、Esc 关闭并回到终端。 */
export function TerminalSearchBar({ result, onSearch, onClose }: TerminalSearchBarProps) {
  const [term, setTerm] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);
  const summary = !term ? "" : result === null || result.count === 0 ? "无结果" : result.index < 0 ? `${result.count}+` : `${result.index + 1}/${result.count}`;
  return (
    <div
      role="search"
      className="bg-popover text-popover-foreground absolute top-2 right-4 z-10 flex items-center gap-1 rounded-md border p-1 shadow-md"
    >
      <input
        ref={inputRef}
        value={term}
        aria-label="在终端中搜索"
        placeholder="搜索"
        className="h-6 w-44 bg-transparent px-1.5 text-xs outline-none"
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
      <TooltipIconButton tooltip="关闭搜索" aria-label="关闭搜索" className="size-6" onClick={onClose}>
        <XIcon aria-hidden="true" />
      </TooltipIconButton>
    </div>
  );
}
