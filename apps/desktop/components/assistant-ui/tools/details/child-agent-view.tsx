import { readAgentTaskResultDisplay } from "../child-agent-display";
import type { DetailView } from "./detail-view";

/**
 * `agent-task-result`（`send_message` / `task_status`）。
 *
 * 对方 Run 的状态不在展示数据里（读任务状态推送）；这里只摆对方名字与查询到的结论。
 * 载荷有效性由 `accepts` 在路由时保证。
 */
export const childAgentView: DetailView = {
  accepts: (data) => readAgentTaskResultDisplay(data) !== null,
  meta: (data) => {
    const result = readAgentTaskResultDisplay(data)!;
    const relation = result.operation === "status" ? "状态" : "已发送";
    return `${result.agentName ?? "任务"} · ${relation}`;
  },
  body: (data) => {
    const result = readAgentTaskResultDisplay(data)!;
    return (
      <div className="space-y-1 text-xs">
        <p className="text-muted-foreground">{result.agentName ?? "任务"} · task {result.taskId}</p>
        {result.endReason && <p className="text-warning">{result.endReason}</p>}
        {result.finalOutput && <p className="whitespace-pre-wrap break-words text-muted-foreground">{result.finalOutput}</p>}
      </div>
    );
  },
};
