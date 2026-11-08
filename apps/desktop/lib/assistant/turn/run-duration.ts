import { useEffect, useState } from "react";

/**
 * 回合耗时的文案：「48秒」「3分钟12秒」「1小时02分」。不足 1 秒按 0 秒计；负值（时钟回拨）按 0。
 */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}小时${String(minutes).padStart(2, "0")}分`;
  if (minutes > 0) return `${minutes}分钟${seconds}秒`;
  return `${seconds}秒`;
}

/** 自 `startedAt`（ISO 时间）到 `now` 过去的毫秒数；解析失败为 null，时钟回拨按 0。 */
export function elapsedSince(startedAt: string | null | undefined, now: number): number | null {
  if (startedAt == null) return null;
  const start = Date.parse(startedAt);
  return Number.isNaN(start) ? null : Math.max(0, now - start);
}

/**
 * 自 `startedAt` 起已过去的毫秒数，`active` 期间每秒刷新；`active` 为假时停止计时并保留最后一次的值
 * （回合落定后脚注改读 `outcome.durationMs`，这里只服务运行中的本地计时）。
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
