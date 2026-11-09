import { useEffect, useRef, type RefObject } from "react";

import type { DomTextSelection } from "@/lib/workspace-files/dom-selection-locator";

/**
 * 跟踪 `host` 里的文本选区（Markdown 渲染视图、PDF 文本层），用于把选中的文字作为引用指针添加到对话。
 *
 * 回调带 DOM Range：渲染视图按块级 `data-source-line` 求源码行范围，PDF 按 `.page[data-page-number]`
 * 求页码（见 `dom-selection-locator`）。只认两端都在 `host` 内的选区：在 host 里折叠选区报 null；
 * 焦点移到别处（如点工具栏按钮、在输入框里打字）时不报，保留最后一次的选区，「添加到对话」才拿得到它。
 * 只用 `selectionchange` 一个来源。
 */
export function useDomSelection(
  host: RefObject<HTMLElement | null>,
  onChange: (selection: DomTextSelection | null) => void,
) {
  const latest = useRef(onChange);
  latest.current = onChange;
  useEffect(() => {
    const handle = () => {
      const selection = window.getSelection();
      const root = host.current;
      if (!root || !selection?.anchorNode || !selection.focusNode) return;
      if (!root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) return;
      const range = selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
      latest.current(range && !range.collapsed ? { text: selection.toString(), range } : null);
    };
    document.addEventListener("selectionchange", handle);
    return () => document.removeEventListener("selectionchange", handle);
  }, [host]);
}
