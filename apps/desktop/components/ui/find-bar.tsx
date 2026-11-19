import { ChevronDownIcon, ChevronRightIcon, ChevronUpIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { formatKeys, matchesShortcut, withShortcut } from "@/lib/keyboard/shortcuts";
import { cn } from "@/lib/utils";

/** 被查找的对象能替换时给出（可编辑的编辑器）；只读内容不给，查找条就没有替换行。 */
export type FindReplace = (term: string, replacement: string, all: boolean) => void;

/**
 * 应用内唯一的查找条（终端、文件源码、Markdown 渲染、PDF、独立文本面共用）：边输入边查，Enter 下一处、
 * Shift+Enter 上一处、Esc 关闭。只管输入与展示；在哪里查、怎么高亮由调用方在 `onSearch` 里决定，并把位置经
 * `result` 交回。浮在内容右上角，由调用方用 `className` 定位。`focusKey` 变化时重新聚焦并全选（再按一次查找键）。
 *
 * 替换行只在调用方给出 `onReplace` 时出现，左侧的展开按钮开合它；`replaceFocusKey` 变化时展开并聚焦替换框
 * （替换快捷键）。替换框里 Enter 替换当前处，快捷键 `replaceAll` 全部替换。查找下一处的快捷键由宿主接住（它知道
 * 焦点在不在自己的范围里），不在这里。
 */
export function FindBar({ label, result, onSearch, onClose, onReplace, focusKey, replaceFocusKey, className, children }: {
  label: string;
  result: FindResult | null;
  onSearch: (term: string, direction: FindDirection) => void;
  onClose: () => void;
  onReplace?: FindReplace;
  focusKey?: number;
  replaceFocusKey?: number;
  className?: string;
  /** 附加操作（如分段文件的「在整个文件中查找」），排在翻页按钮之后。 */
  children?: ReactNode;
}) {
  const [term, setTerm] = useState("");
  const [replacement, setReplacement] = useState("");
  const [replaceOpen, setReplaceOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const replaceRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusKey]);
  const canReplace = onReplace !== undefined;
  // 替换快捷键先展开替换行，等它渲染出来再聚焦；同一次按键只聚焦一次，之后点开合不抢焦点。
  const focusedReplaceKey = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (replaceFocusKey !== undefined && canReplace) setReplaceOpen(true);
  }, [replaceFocusKey, canReplace]);
  useEffect(() => {
    if (!replaceOpen || replaceFocusKey === undefined || focusedReplaceKey.current === replaceFocusKey) return;
    focusedReplaceKey.current = replaceFocusKey;
    replaceRef.current?.focus();
    replaceRef.current?.select();
  }, [replaceOpen, replaceFocusKey]);
  const summary = !term ? "" : result === null || result.count === 0 ? "无结果" : result.index < 0 ? `${result.count}+` : `${result.index + 1}/${result.count}`;
  const escape = (event: React.KeyboardEvent) => {
    if (event.key !== "Escape") return false;
    event.preventDefault();
    onClose();
    return true;
  };
  return (
    <div
      role="search"
      className={cn("bg-popover text-popover-foreground flex flex-col gap-1 rounded-md border p-1 shadow-md", className)}
    >
      <div className="flex items-center gap-1">
        {onReplace && (
          <TooltipIconButton tooltip={withShortcut(replaceOpen ? "收起替换" : "替换", "openReplace")} aria-label="切换替换" aria-expanded={replaceOpen}
            className="size-6" onClick={() => setReplaceOpen(!replaceOpen)}>
            {replaceOpen ? <ChevronDownIcon aria-hidden="true" /> : <ChevronRightIcon aria-hidden="true" />}
          </TooltipIconButton>
        )}
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
            if (escape(event)) return;
            if (event.key === "Enter") {
              event.preventDefault();
              onSearch(term, event.shiftKey ? "previous" : "next");
            }
          }}
        />
        <span className="text-muted-foreground min-w-10 text-right text-2xs tabular-nums" aria-live="polite">{summary}</span>
        <TooltipIconButton tooltip={`上一个（${formatKeys("shift+enter")}）`} aria-label="上一个匹配" className="size-6" onClick={() => onSearch(term, "previous")}>
          <ChevronUpIcon aria-hidden="true" />
        </TooltipIconButton>
        <TooltipIconButton tooltip={`下一个（${formatKeys("enter")}）`} aria-label="下一个匹配" className="size-6" onClick={() => onSearch(term, "next")}>
          <ChevronDownIcon aria-hidden="true" />
        </TooltipIconButton>
        {children}
        <TooltipIconButton tooltip={`关闭搜索（${formatKeys("escape")}）`} aria-label="关闭搜索" className="size-6" onClick={onClose}>
          <XIcon aria-hidden="true" />
        </TooltipIconButton>
      </div>
      {onReplace && replaceOpen && (
        <div className="flex items-center gap-1 pl-7">
          <input
            ref={replaceRef}
            value={replacement}
            aria-label="替换为"
            placeholder="替换为"
            className="h-6 w-44 min-w-0 bg-transparent px-1.5 text-xs outline-none"
            onChange={(event) => setReplacement(event.target.value)}
            onKeyDown={(event) => {
              if (escape(event)) return;
              if (matchesShortcut(event, "replaceAll")) {
                event.preventDefault();
                onReplace(term, replacement, true);
              } else if (event.key === "Enter") {
                event.preventDefault();
                onReplace(term, replacement, false);
              }
            }}
          />
          <Button size="xs" variant="ghost" disabled={!term} title={`替换当前处（${formatKeys("enter")}）`} onClick={() => onReplace(term, replacement, false)}>替换</Button>
          <Button size="xs" variant="ghost" disabled={!term} title={withShortcut("全部替换", "replaceAll")} onClick={() => onReplace(term, replacement, true)}>全部替换</Button>
        </div>
      )}
    </div>
  );
}
