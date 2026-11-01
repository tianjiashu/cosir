import type { ReactNode } from "react";

import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

/** kind 视图渲染时拿到的调用上下文：按 Task 寻址的命令、只读面开关与 assistant-ui 侧的原始参数。 */
export type CallViewContext = {
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
 * 一种 `display_data.kind` 在统一调用行上的投影：元信息（行内一句摘要）、展开体与行尾动作。
 *
 * 全部是纯函数，只读后端已投影的展示数据；返回的元素可以是带 hook 的组件（委派行要订阅任务状态、终端行
 * 要订阅会话列表）。失败与取消由调用行统一处理（元信息换成后端短提示、不可展开），视图只会拿到成功或进行中
 * 的载荷。有展开体的行整行是折叠按钮，`meta` 里不能再放可点击元素，可点的东西放 `actions`（渲染在按钮之外）。
 */
export type CallView = {
  /** 该 kind 的专用载荷校验；不通过时按无 kind 处理（只有标题与状态），`meta` / `body` 因此可信任载荷形状。 */
  accepts?: (data: Record<string, unknown>) => boolean;
  meta?: (data: Record<string, unknown>, context: CallViewContext) => ReactNode;
  /** 返回 null 表示这份载荷没有值得展开的内容，行因此不可展开。 */
  body?: (data: Record<string, unknown>, context: CallViewContext) => ReactNode;
  /** 展开体的默认开合（用户没点过时）；缺省收起。命令行运行中展开、退出码非 0 时展开就靠它。 */
  defaultOpen?: (data: Record<string, unknown>, context: CallViewContext) => boolean;
  /** 行尾动作（打开子任务、停止命令、在终端中查看），渲染在折叠按钮之外。 */
  actions?: (data: Record<string, unknown>, context: CallViewContext) => ReactNode;
};
