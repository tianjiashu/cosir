import type { ContextTurnRun } from "@/lib/api/context-messages";
import { formatTime } from "./message-model";

/**
 * 按回合查询时，这一回合所属运行的状态事实。结局与用量属于整个运行（一个运行可以有多个回合），
 * 所以单独成行、注明归属；起止时间用来按 Session 与时间窗检索后端日志。
 */
export function TurnRunSummary({ run }: { run: ContextTurnRun }) {
  const total = run.usage?.total_tokens;
  return (
    <p
      className="shrink-0 border-b px-5 py-2 text-xs text-muted-foreground"
      data-testid="context-turn-run"
    >
      所属运行（整个运行共享）：{run.status}
      {run.end_reason ? ` · ${run.end_reason}` : ""}
      {run.error_code ? ` · ${run.error_code}` : ""}
      {typeof total === "number" ? ` · ${total} tokens` : ""}
      {` · ${formatTime(run.started_at, true)} – ${formatTime(run.updated_at, true)}`}
    </p>
  );
}
