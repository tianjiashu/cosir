import { useLayoutEffect, useRef, useState, type RefObject } from "react";

export type ElementSize = { width: number; height: number };

/**
 * 观察元素的布局尺寸（含边框，取自 `getBoundingClientRect`）。
 *
 * 挂载时先在 layout effect 里同步量一次再挂 ResizeObserver：只靠观察者回调时首帧尺寸为 0，
 * 虚拟列表会先空白一帧再出现内容。尺寸不变时沿用旧对象，不触发多余渲染。
 *
 * 无参时使用返回的 ref：调用方须把它绑定到自身渲染的固定节点，不能在挂载期间更换节点。
 * 外部目标须传响应式的实际元素（如视口 store 的元素），绑定或换元素时清理旧订阅并重新测量；
 * `null` 表示尚未绑定或停止观察，保留最近一次测量值。不能传 RefObject，其 current 变化不会通知 React。
 */
export function useElementSize<T extends HTMLElement>(target?: T | null): [RefObject<T | null>, ElementSize] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState<ElementSize>({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const element = target === undefined ? ref.current : target;
    if (!element) return;
    const measure = () => {
      const { width, height } = element.getBoundingClientRect();
      setSize((current) => (current.width === width && current.height === height ? current : { width, height }));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, target]);
  return [ref, size];
}
