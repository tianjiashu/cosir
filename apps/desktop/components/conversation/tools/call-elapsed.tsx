import { useElapsedSince } from "@/hooks/use-elapsed-since";
import { formatDuration } from "@/lib/format/duration";

/** 短于这个时长不显示：几百毫秒就结束的调用不该冒出时长，只有「跑了一阵」才值得说。 */
const SHOW_AFTER_MS = 3_000;

/**
 * 调用是否已「跑了一阵」：与 `CallElapsed` 同一门槛，让时长与运行中的操作（如终端的转到后台、停止）同时出现，
 * 很快结束的调用不会闪一下按钮。没有可用的运行起点（冷重建后，或解析失败）时按已跑了一阵算：重新连上看到的调用不会是刚启动的，
 * 照搬「不足门槛不显示」会让操作永远不出现。
 */
export function useCallRanAWhile(startedAt: string | undefined): boolean {
  const elapsed = useElapsedSince(startedAt, startedAt !== undefined);
  return startedAt === undefined || elapsed === null || elapsed >= SHOW_AFTER_MS;
}

/**
 * 调用行标题后的运行时长：证明调用还活着、跑了多久，结束即消失（成功不说话）。
 *
 * 起点是 part 的 `startedAt`（后端在进入 running 时取状态事件时刻，离开 running 清空），所以只有
 * 前台执行中的调用会计时；后台工作的计时在侧栏看板。独立组件让每秒刷新只重渲染这一小段，不牵动整行。
 */
export function CallElapsed({ startedAt }: { startedAt: string | undefined }) {
  const elapsed = useElapsedSince(startedAt, startedAt !== undefined);
  if (elapsed === null || elapsed < SHOW_AFTER_MS) return null;
  return (
    <span data-slot="call-elapsed" className="text-muted-foreground shrink-0 text-xs tabular-nums">
      {formatDuration(elapsed)}
    </span>
  );
}
