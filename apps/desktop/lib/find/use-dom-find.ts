import { useCallback, useEffect, useRef, type RefObject } from "react";

import { MAX_FIND_MATCHES, nextMatchIndex, type FindDirection, type FindResult } from "./next-match";

const MATCHES = "cosir-find";
const CURRENT = "cosir-find-current";

/** 高亮注册表是整个文档共享的：同一时刻只有一个可见阅读器在查找，关闭或卸载时清空。 */
function clearHighlights() {
  CSS.highlights.delete(MATCHES);
  CSS.highlights.delete(CURRENT);
}

/**
 * 在一段已渲染的 DOM（Markdown 预览）里就地查找（不区分大小写）。
 *
 * 用 CSS Custom Highlight API 标出命中：不改 DOM，不打断 React 渲染与文本选择；颜色在 globals.css。
 * 每次查找都重新遍历文本节点，内容异步变化（Mermaid、图片）后再按一次 Enter 即对齐。跨元素断开的文字
 * （如一半加粗）不算命中，这是逐个文本节点匹配的取舍。
 */
export function useDomFind(host: RefObject<HTMLElement | null>) {
  const last = useRef<{ term: string; anchor: number }>({ term: "", anchor: 0 });
  useEffect(() => clearHighlights, []);
  const find = useCallback((term: string, direction: FindDirection): FindResult | null => {
    const root = host.current;
    if (!root) return null;
    const fresh = last.current.term !== term;
    clearHighlights();
    if (!term) {
      last.current = { term, anchor: 0 };
      return { index: -1, count: 0 };
    }
    const needle = term.toLowerCase();
    const ranges: Range[] = [];
    const starts: number[] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let base = 0;
    for (let node = walker.nextNode(); node && ranges.length < MAX_FIND_MATCHES; node = walker.nextNode()) {
      const text = (node.nodeValue ?? "").toLowerCase();
      for (let at = text.indexOf(needle); at >= 0 && ranges.length < MAX_FIND_MATCHES; at = text.indexOf(needle, at + needle.length)) {
        const range = document.createRange();
        range.setStart(node, at);
        range.setEnd(node, at + needle.length);
        ranges.push(range);
        starts.push(base + at);
      }
      base += text.length;
    }
    if (!ranges.length) {
      last.current = { term, anchor: 0 };
      return { index: -1, count: 0 };
    }
    const index = nextMatchIndex(starts, last.current.anchor, direction, fresh);
    last.current = { term, anchor: starts[index] };
    CSS.highlights.set(MATCHES, new Highlight(...ranges.filter((_, i) => i !== index)));
    CSS.highlights.set(CURRENT, new Highlight(ranges[index]));
    ranges[index].startContainer.parentElement?.scrollIntoView({ block: "center" });
    return ranges.length >= MAX_FIND_MATCHES ? { index: -1, count: ranges.length } : { index, count: ranges.length };
  }, [host]);
  const clear = useCallback(() => {
    last.current = { term: "", anchor: 0 };
    clearHighlights();
  }, []);
  return { find, clear };
}
