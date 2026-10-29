import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { readChildAgentResultDisplay } from "../child-agent-display";
import type { DetailView } from "./detail-view";

/**
 * `child-agent-result`（`child_agent_send` / `child_agent_status`）。
 *
 * 摘要里的状态是子 Task 最新 Run 的状态（含 suspended），用 Run 状态文案而不是工具调用
 * 状态；行尾的工具状态只表示这次查询 / 发送本身。renderer 注册表已保证载荷有效。
 */
export const childAgentView: DetailView = {
  summary: (data) => {
    const result = readChildAgentResultDisplay(data)!;
    return `${result.agentName ?? "子 Agent"} · 子 Agent：${taskRunStatusPresentation(result.status, true)?.label}`;
  },
  body: (data) => {
    const result = readChildAgentResultDisplay(data)!;
    return (
      <div className="space-y-1 text-xs">
        <p className="text-muted-foreground">
          {result.agentName ?? "子 Agent"} · task {result.childTaskId} · {taskRunStatusPresentation(result.status, true)?.label}
        </p>
        {result.endReason && <p className="text-warning">{result.endReason}</p>}
        {result.finalOutput && <p className="whitespace-pre-wrap break-words text-muted-foreground">{result.finalOutput}</p>}
      </div>
    );
  },
};
