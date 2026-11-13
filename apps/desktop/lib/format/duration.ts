/**
 * 运行时长的文案与计算：回合标题、工具调用行与后台看板共用这一种格式（「48秒」「3分钟12秒」「1小时02分」）。
 * 不足 1 秒按 0 秒计；负值（时钟回拨）按 0。
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
