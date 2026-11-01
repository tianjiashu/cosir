import { useEffect, useRef, type ReactNode } from "react";

import "./backdrop.css";

/**
 * 新对话页的点阵背景：静态点阵底纹，鼠标附近再透出一圈品牌色点阵。纯装饰，不读写任何应用状态。
 *
 * 刻意不用 canvas / 逐帧动画：新对话页常是长时间停留的空闲页，持续绘制会一直占用 GPU；
 * 浏览器面板铺满时对话区只是 `hidden` 不卸载，逐帧绘制还得另做可见性暂停。这里底纹是纯 CSS，
 * 品牌色层只在指针移动时经 rAF 合并更新两个 CSS 变量，静止或看不见时没有任何开销。
 * 层的样式与「减少动态效果」处理在同名 CSS 里。
 */
export function NewConversationBackdrop({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const frame = useRef(0);
  const pointer = useRef({ x: 0, y: 0 });

  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  const applyPointer = () => {
    frame.current = 0;
    const element = ref.current;
    if (!element) return;
    element.style.setProperty("--backdrop-spot-x", `${pointer.current.x}px`);
    element.style.setProperty("--backdrop-spot-y", `${pointer.current.y}px`);
    element.dataset.spot = "on";
  };

  return (
    <div
      ref={ref}
      className="new-conversation-backdrop"
      onPointerMove={(event) => {
        if (event.pointerType !== "mouse") return;
        const rect = event.currentTarget.getBoundingClientRect();
        pointer.current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
        if (!frame.current) frame.current = requestAnimationFrame(applyPointer);
      }}
      onPointerLeave={(event) => {
        cancelAnimationFrame(frame.current);
        frame.current = 0;
        delete event.currentTarget.dataset.spot;
      }}
    >
      <div className="new-conversation-backdrop__dots" aria-hidden="true" />
      <div className="new-conversation-backdrop__spot" aria-hidden="true" />
      {children}
    </div>
  );
}
