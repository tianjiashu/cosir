import { useEffect, useRef, useState, type ReactNode } from "react";

import { FindBar } from "@/components/ui/find-bar";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { matchesShortcut } from "@/lib/keyboard/shortcuts";
import { cn } from "@/lib/utils";

/**
 * 查找条要查的那个编辑器。由宿主经 ref 现取：编辑器尚未建好时返回 null。只读查看器不给 `replace`，
 * 查找条就没有替换行、替换键也不响应。
 */
export type EditorFindTarget = {
  find: (term: string, direction: FindDirection) => FindResult | null;
  /** 文本变了以后重算命中，不移动选区。 */
  refresh: (term: string) => FindResult | null;
  replace?: (term: string, replacement: string, all: boolean) => FindResult | null;
};

/**
 * 独立文本面（观测页详情、Skill 详情、配置编辑器的源码）的查找：查找键（登记表 `findInText`）打开应用的查找条，
 * 在 `target` 的编辑器里查（`editor-find.ts`）；查找条开着时接住查找下一处 / 上一处与替换键。文件面板有自己的
 * 整文件查找（`useTextFind`），不用它。
 *
 * 按键在捕获阶段接住，焦点在编辑器或查找条里都能用；再按一次查找键重新聚焦并全选。`contentKey` 变化（换了
 * 文本或编辑了内容）时按当前词刷新命中与计数，不移动选区。关闭时清掉高亮、把焦点交回打开前所在的元素。
 */
export function EditorFindScope({ target, contentKey, label = "在文本中查找", className, barClassName = "top-2 right-4", children }: {
  target: EditorFindTarget;
  contentKey?: unknown;
  label?: string;
  className?: string;
  /** 查找条在右上角的位置，给同一角上的其他按钮让位。 */
  barClassName?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [focusKey, setFocusKey] = useState(0);
  const [replaceFocusKey, setReplaceFocusKey] = useState<number | undefined>(undefined);
  const [result, setResult] = useState<FindResult | null>(null);
  const term = useRef("");
  const returnFocus = useRef<HTMLElement | null>(null);
  const latestTarget = useRef(target);
  latestTarget.current = target;
  useEffect(() => {
    if (open && term.current) setResult(latestTarget.current.refresh(term.current));
  }, [open, contentKey]);
  const search = (next: string, direction: FindDirection) => {
    term.current = next;
    setResult(target.find(next, direction));
  };
  const show = () => {
    if (!open) returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOpen(true);
  };
  const close = () => {
    target.find("", "next");
    term.current = "";
    setResult(null);
    setOpen(false);
    setReplaceFocusKey(undefined);
    returnFocus.current?.focus();
  };
  const replace = target.replace;
  return (
    <div
      className={cn("relative", className)}
      onKeyDownCapture={(event) => {
        let handled = true;
        if (matchesShortcut(event, "findInText")) {
          show();
          setFocusKey((key) => key + 1);
        } else if (matchesShortcut(event, "openReplace") && replace) {
          show();
          setReplaceFocusKey((key) => (key ?? 0) + 1);
        } else if (open && term.current && matchesShortcut(event, "findNext")) search(term.current, "next");
        else if (open && term.current && matchesShortcut(event, "findPrevious")) search(term.current, "previous");
        else handled = false;
        if (!handled) return;
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      {children}
      {open && (
        <FindBar label={label} result={result} onSearch={search} onClose={close} focusKey={focusKey} replaceFocusKey={replaceFocusKey}
          onReplace={replace && ((find, replacement, all) => { term.current = find; setResult(replace(find, replacement, all)); })}
          className={cn("absolute z-10 max-w-[calc(100%-2rem)]", barClassName)} />
      )}
    </div>
  );
}
