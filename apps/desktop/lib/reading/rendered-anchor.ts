import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

import type { ReadingAnchor } from "./reading-anchor";

/**
 * Markdown 渲染视图里的阅读位置：块级元素带源码行号（`data-source-line`，`rehypeSourceLines` 打上），
 * 按它在渲染结果与源码行之间换算。
 */

type Block = { line: number; top: number };

function blocksOf(host: HTMLElement): Block[] {
  const base = host.getBoundingClientRect().top - host.scrollTop;
  return [...host.querySelectorAll<HTMLElement>("[data-source-line]")].map((element) => ({
    line: Number(element.dataset.sourceLine),
    top: element.getBoundingClientRect().top - base,
  }));
}

/** 视口顶部所在的块：顶部不低于视口顶部的最后一个块（嵌套时取更里层的那个）。 */
export function renderedAnchorOf(host: HTMLElement): ReadingAnchor {
  let found: Block | null = null;
  for (const block of blocksOf(host)) if (block.top <= host.scrollTop + 1) found = block;
  return found ? { line: found.line, offset: Math.max(0, host.scrollTop - found.top) } : { line: 1, offset: host.scrollTop };
}

/** 滚到阅读位置：源码行不在任何块的开头时，停在它所在的那一块（行号不超过它的最后一块）。 */
export function scrollRenderedTo(host: HTMLElement, anchor: ReadingAnchor): void {
  let found: Block | null = null;
  for (const block of blocksOf(host)) if (block.line <= anchor.line && (!found || block.line >= found.line)) found = block;
  host.scrollTop = found ? found.top + (found.line === anchor.line ? anchor.offset : 0) : 0;
}

/**
 * 打开渲染视图时恢复阅读位置，之后用户的滚动经 `onAnchorChange` 记下。
 *
 * 只在打开时按块算一次：之后图片、Mermaid 图、代码高亮异步把上方内容撑高，由浏览器的滚动锚定
 * （CSS `overflow-anchor`，默认开启）保持可见的那一块不动，这里不再自己守。不要在滚动容器或内容上关掉它。
 */
export function useRenderedAnchor(
  host: RefObject<HTMLElement | null>,
  anchor: ReadingAnchor | undefined,
  onAnchorChange: (anchor: ReadingAnchor) => void,
) {
  const initial = useRef(anchor);
  const latest = useRef(onAnchorChange);
  latest.current = onAnchorChange;
  useLayoutEffect(() => {
    if (host.current && initial.current) scrollRenderedTo(host.current, initial.current);
  }, [host]);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    let frame = 0;
    // 按帧合并：滚动事件很密，换算要遍历块。
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => latest.current(renderedAnchorOf(element)));
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      element.removeEventListener("scroll", onScroll);
    };
  }, [host]);
}
