import { useSyncExternalStore } from "react";

/** 订阅一个 CSS 媒体查询的匹配结果；SSR / 无 window 时视为不匹配。 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}
