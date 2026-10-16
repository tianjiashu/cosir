"use client";

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * 应用内全屏的挂载边界：全屏时脱离页面的裁剪、定位包含块和堆叠上下文。
 * 始终复用同一个 portal 容器，只移动容器，避免切换挂载目标导致编辑器重建、
 * 视口及输入状态丢失。调用方负责全屏尺寸、退出交互与弹窗层级。
 */
export function FullscreenPortal({ active, children }: { active: boolean; children: ReactNode }) {
  const inlineRef = useRef<HTMLDivElement>(null);
  const [host] = useState(() => {
    const element = document.createElement("div");
    element.style.display = "contents";
    return element;
  });

  useLayoutEffect(() => {
    // 移动 DOM 会使浏览器丢失焦点；恢复同一节点，不改变编辑器的选择与输入状态。
    const focused = document.activeElement;
    const restoreFocus = focused instanceof HTMLElement && host.contains(focused);
    const parent = active ? document.body : inlineRef.current!;
    parent.appendChild(host);
    if (restoreFocus) focused.focus({ preventScroll: true });
  }, [active, host]);

  useLayoutEffect(() => () => host.remove(), [host]);

  return <div ref={inlineRef} className="contents">{createPortal(children, host)}</div>;
}
