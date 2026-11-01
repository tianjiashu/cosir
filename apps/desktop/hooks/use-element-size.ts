import { useLayoutEffect, useRef, useState, type RefObject } from "react";

export type ElementSize = { width: number; height: number };

/**
 * 观察元素的布局尺寸（含边框，取自 `getBoundingClientRect`）。
 *
 * 挂载时先在 layout effect 里同步量一次再挂 ResizeObserver：只靠观察者回调时首帧尺寸为 0，
 * 虚拟列表会先空白一帧再出现内容。只观察首次挂载时 ref 指向的元素，挂载期间不应更换元素。
 * 尺寸不变时沿用旧对象，不触发多余渲染。
 *
 * 传入 `target` 时观察调用方已有的元素（如对话视口，它的 ref 还另有用途），否则返回一个新 ref 供挂载。
 */
export function useElementSize<T extends HTMLElement>(target?: RefObject<T | null>): [RefObject<T | null>, ElementSize] {
  const own = useRef<T>(null);
  const ref = target ?? own;
  const [size, setSize] = useState<ElementSize>({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const { width, height } = element.getBoundingClientRect();
      setSize((current) => (current.width === width && current.height === height ? current : { width, height }));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return [ref, size];
}
