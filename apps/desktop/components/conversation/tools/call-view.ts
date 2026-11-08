import type { ReactNode } from "react";

import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

/** kind 视图渲染时拿到的调用上下文：按 Task 寻址的命令、只读面开关与 assistant-ui 侧的原始参数。 */
export type CallViewContext = {
  /** 后台登记的收尾状态；工具 pending 本身无法区分仍在执行与待并入。 */
  workStatus?: import("@/lib/assistant/contract").BackgroundTaskEntry["status"];
  /** 时间线保持摘要；详情面复用完整工具内容。 */
  surface?: "timeline" | "details";
  /** 继承记录没有执行归属：仅使用持久化结果，不装配源实体的实时订阅或操作。 */
  inherited?: boolean;
  taskId: number;
  toolCallId: string;
  artifact: ToolArtifact;
  args: Record<string, unknown>;
  /** 只读展示面（子 Agent 阅读器、后台看板）不提供写入交互（停止、交还、转到后台）。 */
  interactive: boolean;
  /** 用户已请求停止整个 Task 的当前活动，工具级停止入口随之收起。 */
  taskCancelling: boolean;
};

/**
 * 这次调用是否归侧栏：后台工作在时间线只留一行，详情与操作只在看板，点行定位过去（`resolveCallView` 一处
 * 裁剪，视图不用判断）。只读阅读器看的是别的 Task、继承记录没有看板条目，这两处当前 Task 的侧栏里都找不到
 * 它，仍就地展开。
 */
export function detailsLiveInRail({ artifact, surface = "timeline", interactive, inherited = false }: CallViewContext): boolean {
  return artifact.background && surface === "timeline" && interactive && !inherited;
}

/** 一次解析得到的行展示；detail 存在即表示可展开，render 仅由可见详情子树调用。 */
export type CallPresentation = {
  /** 工具状态看不出的实体异常（子任务失败、Team 被驳回）；工具自身失败、取消、等你操作优先。正常时渲染为空。 */
  status?: ReactNode;
  /** 取代目录算出的目标文字，只用于让目标可交互（路径可点开），内容仍是同一个目标。 */
  target?: ReactNode;
  /** 只写调用结果（大小、数量）；目标已在行上，元信息不再重复路径、模式或查询词。 */
  meta?: ReactNode;
  actions?: ReactNode;
  /** 独立弹层随调用存活，不随行内详情的开合卸载。 */
  overlay?: ReactNode;
  detail?: { defaultOpen?: boolean; render: () => ReactNode };
};

/** kind 的纯解析器；无效专用载荷返回 null。不要调用 hooks，状态订阅由返回的组件拥有。 */
export type CallView = (data: Record<string, unknown>, context: CallViewContext) => CallPresentation | null;
