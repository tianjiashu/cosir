import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import type { QuoteCapsule } from "@/lib/assistant/capsules";

/** 浮条锚点：选区末端的矩形，随滚动现算（floating-ui 的虚拟元素）。 */
export type SelectionAnchor = { getBoundingClientRect: () => DOMRect; contextElement?: Element };

/** 一次已确定的选区：能换成的摘录与浮条锚点；`range` 只在 DOM 选区时有（评论框打开期间用它画替身高亮）。 */
export type PendingSelection = { quote: QuoteCapsule; anchor: SelectionAnchor; range: Range | null };

export type TextSelectionState = {
  pending: PendingSelection | null;
  commenting: boolean;
  /** 选区来源报告选区确定或消失；评论框打开期间选区归评论框，报告被忽略。 */
  report: (selection: PendingSelection | null) => void;
  /** 打开评论框；没有选区时返回 false（调用方可退回别的动作，如阅读器添加整个文件）。 */
  startComment: () => boolean;
  close: () => void;
};

/**
 * 一个界面的「选中 → 对话」状态：选区来源（DOM 或编辑器）报告选区，浮条与快捷键据此显示与打开评论框。
 * 浮条显隐只看这里的 `pending`——选区这一个事实，不另设「点到外面就关」的第二个判定。
 */
export function useTextSelection(): TextSelectionState {
  const [pending, setPending] = useState<PendingSelection | null>(null);
  const [commenting, setCommenting] = useState(false);
  // 选区监听在 effect 里，读最新状态。
  const latest = useRef({ pending, commenting });
  latest.current = { pending, commenting };
  const report = useCallback((selection: PendingSelection | null) => {
    if (!latest.current.commenting) setPending(selection);
  }, []);
  const startComment = useCallback(() => {
    if (!latest.current.pending) return false;
    latest.current.commenting = true;
    setCommenting(true);
    return true;
  }, []);
  const close = useCallback(() => {
    latest.current.commenting = false;
    setCommenting(false);
    setPending(null);
  }, []);
  return useMemo(() => ({ pending, commenting, report, startComment, close }), [pending, commenting, report, startComment, close]);
}

/** 文件阅读器里的选区状态，交给工具栏的「添加到对话」与 Cmd+L（有选区打开评论框，没有就加整个文件）。 */
export const TextSelectionContext = createContext<TextSelectionState | null>(null);
export const useTextSelectionContext = () => useContext(TextSelectionContext);

/**
 * DOM 选区来源：`host` 里的选区在松开鼠标或键盘时确定（拖选过程中不报，浮条不闪），折叠即消失；`resolve`
 * 把选区换成摘录，换不出（选到界面文字、跨了不该跨的边界）就当没有选区。
 *
 * 选中的行内代码同时打上 `data-selected`：浏览器不给行内元素的内边距上选区色，代码块两侧会留灰边，
 * 由样式按这个标记整块上色（`globals.css` 的 `--selection`）。
 */
export function useDomSelectionSource(
  host: HTMLElement | null,
  state: TextSelectionState,
  resolve: (range: Range) => QuoteCapsule | null,
) {
  const latest = useRef({ resolve, report: state.report });
  latest.current = { resolve, report: state.report };
  useEffect(() => {
    if (!host) return;
    const current = () => {
      const selection = window.getSelection();
      const range = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
      return range && !range.collapsed && host.contains(range.commonAncestorContainer) ? range : null;
    };
    const settle = (event: Event) => {
      if (event.target instanceof Node && !host.contains(event.target)) return;
      const range = current();
      const quote = range && latest.current.resolve(range);
      latest.current.report(range && quote ? { quote, range: range.cloneRange(), anchor: rangeAnchor(range.cloneRange(), host) } : null);
    };
    let frame = 0;
    const change = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => markInlineCode(host, current()));
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) latest.current.report(null);
    };
    document.addEventListener("mouseup", settle);
    document.addEventListener("keyup", settle);
    document.addEventListener("selectionchange", change);
    return () => {
      cancelAnimationFrame(frame);
      markInlineCode(host, null);
      document.removeEventListener("mouseup", settle);
      document.removeEventListener("keyup", settle);
      document.removeEventListener("selectionchange", change);
    };
  }, [host]);
}

function rangeAnchor(range: Range, host: HTMLElement): SelectionAnchor {
  return {
    getBoundingClientRect: () => {
      const rects = range.getClientRects();
      return rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
    },
    contextElement: host,
  };
}

function markInlineCode(host: HTMLElement, range: Range | null) {
  for (const code of host.querySelectorAll<HTMLElement>('[data-streamdown="inline-code"]')) {
    code.toggleAttribute("data-selected", !!range && range.intersectsNode(code));
  }
}
