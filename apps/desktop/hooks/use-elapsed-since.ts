import { useEffect, useState } from "react";

import { elapsedSince } from "@/lib/format/duration";

/**
 * 自 `startedAt` 起已过去的毫秒数，`active` 期间每秒刷新；`active` 为假时停止计时并保留最后一次的值
 * （终态的耗时由调用方改读后端冻结的值，这里只服务运行中的本地计时）。
 * `startedAt` 解析失败时返回 null，调用方不显示耗时。
 */
export function useElapsedSince(startedAt: string | null | undefined, active: boolean): number | null {
  const [elapsed, setElapsed] = useState<number | null>(() => elapsedSince(startedAt, Date.now()));

  useEffect(() => {
    const tick = () => setElapsed(elapsedSince(startedAt, Date.now()));
    tick();
    if (!active) return;
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [active, startedAt]);

  return elapsed;
}
