import type { TransportToolDisplayData, TransportToolStatus } from "@/lib/assistant/contract";

/**
 * 工具调用 part 挂在 assistant-ui `artifact` 上的 UI 适配数据。
 *
 * assistant-ui 的 `ToolCallMessagePart` 只承载 args / argsText / result / isError，后端的生命周期五态、
 * 等待用户与展示数据放不进去，converter 把它们原样收进 `artifact`；状态与展示数据只读这里，不从 `args` /
 * `result` 反推（图标、动词与调用标题按工具名查 `tool-catalog.ts`）。它不会回写后端，也不是对话事实。
 */
export type ToolArtifact = {
  backendStatus: TransportToolStatus;
  /** 是否在等用户决定；投影自 part `awaitingUser`，不从 `pending` 推断。 */
  awaitingUser: boolean;
  display_data: TransportToolDisplayData | null;
  team_run_id?: number;
  error: string | null;
  errorCode: string | null;
  childTaskId?: number;
  agent_role?: string;
  /** 延迟结果的后台占位（投影自 part `background`，结算后保留）：所有工具共用的「在后台」事实。 */
  background: boolean;
};

const EMPTY_ARTIFACT: ToolArtifact = {
  backendStatus: "unknown",
  awaitingUser: false,
  display_data: null,
  error: null,
  errorCode: null,
  background: false,
};

function positiveInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

/**
 * 从 assistant-ui part 的 `artifact` 读回 `ToolArtifact`。`artifact` 在 assistant-ui 侧是 `unknown`，
 * 这里是前端自己写、自己读的唯一一处形状收口；未知状态值一律归为 `unknown`，不得被当成 pending 或 completed。
 */
export function readToolArtifact(value: unknown): ToolArtifact {
  if (typeof value !== "object" || value === null) return EMPTY_ARTIFACT;
  const candidate = value as Partial<ToolArtifact>;
  const status = candidate.backendStatus;
  const backendStatus: TransportToolStatus = status === "pending" || status === "running" || status === "completed" || status === "failed" || status === "cancelled"
    ? status
    : "unknown";
  return {
    backendStatus,
    awaitingUser: candidate.awaitingUser === true,
    display_data: candidate.display_data ?? null,
    team_run_id: positiveInteger(candidate.team_run_id),
    // converter 已按后端 part.error 收口失败 / 取消提示，这里只透传。
    error: typeof candidate.error === "string" ? candidate.error : null,
    errorCode: typeof candidate.errorCode === "string" ? candidate.errorCode : null,
    childTaskId: positiveInteger(candidate.childTaskId),
    agent_role: typeof candidate.agent_role === "string" && candidate.agent_role.trim() ? candidate.agent_role : undefined,
    background: candidate.background === true,
  };
}
