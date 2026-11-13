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
  intent?: string | null;
  presentation: "activity" | "standalone" | "quiet";
  /** 是否在等用户决定；投影自 part `awaitingUser`，不从 `pending` 推断。 */
  awaitingUser: boolean;
  display_data: TransportToolDisplayData | null;
  error: string | null;
  errorCode: string | null;
  childTaskId?: number;
  agent_role?: string;
  /** 后台结果的后台占位（投影自 part `background`，结算后保留）：所有工具共用的「在后台」事实。 */
  background: boolean;
  /** 运行起点（投影自 part `startedAt`，只在 running 期间有值），只服务运行时长的本地计时。 */
  startedAt?: string;
};

/**
 * 恢复 SDK 擦除的项目自有类型。artifact 唯一由 converter 创建，SDK 原样传递；
 * 不在读取端复制、补字段或重判状态，保留原对象身份以支持工具行的 memo。
 */
export function readToolArtifact(value: unknown): ToolArtifact {
  return value as ToolArtifact;
}
