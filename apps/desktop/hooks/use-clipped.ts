import { useLayoutEffect, useState, type DependencyList, type RefObject } from "react";

/**
 * 限高（`line-clamp` / `max-height` + `overflow-hidden`）的元素此刻是否截掉了内容，用来决定要不要
 * 给出「显示全部」入口；内容放得下时不出现入口。
 *
 * 只在 `active`（处于收起态）时测量，展开后保留最近一次结果——展开态不限高，量出来恒为 false，
 * 会让「收起」入口跟着消失。ResizeObserver 只在盒子尺寸变化（列宽变化、隐藏后重新显示）时
 * 重新测量；限高盒子里内容变长、盒子本身不变，所以内容身份要放进 `deps`。隐藏时量到 0，
 * 重新显示时观察者会再触发一次。
 */
export function useClipped(ref: RefObject<HTMLElement | null>, active: boolean, deps: DependencyList): boolean {
  const [clipped, setClipped] = useState(false);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || !active) return;
    const measure = () => setClipped(element.scrollHeight > element.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, active, ...deps]);
  return clipped;
}
