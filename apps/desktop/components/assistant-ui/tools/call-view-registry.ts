import type { ToolArtifact } from "@/lib/assistant/tool-artifact";
import type { CallView } from "./call-view";
import { DETAIL_VIEWS } from "./details/detail-views";
import { asRecord } from "./display-utils";
import { browserPageView } from "./views/browser-page-view";
import { agentTaskResultView, delegationView, forkTaskView } from "./views/child-task-views";
import { fileChangesView } from "./views/file-changes-view";
import { terminalResultView } from "./views/terminal-result-view";
import { terminalSessionView } from "./views/terminal-session-view";

/**
 * `display_data.kind` → 调用行视图。这是前端唯一的工具展示路由：只按 kind，不按工具名、不按静态展示声明。
 * 新增一种工具展示只需在这里登记一个 kind；产物类 kind（草稿、Team 审批）不在这里——它们在产物区 /
 * 等待区各自成卡，过程里的那一行只有标题与状态。
 */
export const CALL_VIEWS: Readonly<Record<string, CallView>> = {
  ...DETAIL_VIEWS,
  "terminal-result": terminalResultView,
  "terminal-session": terminalSessionView,
  "file-changes": fileChangesView,
  "delegation-result": delegationView,
  "fork-task-result": forkTaskView,
  "agent-task-result": agentTaskResultView,
  "browser-page": browserPageView,
};

export type ResolvedCallView = { view: CallView; data: Record<string, unknown> };

/**
 * 按展示数据的 kind 取视图；没有 kind（排队、运行中、只有静态声明）、未知 kind 或专用载荷校验不通过时
 * 返回 null，调用行退化为只有标题与状态——不会猜测工具名，也不会让不受信任的载荷绕过校验。
 */
export function resolveCallView(artifact: ToolArtifact): ResolvedCallView | null {
  const data = asRecord(artifact.display_data);
  if (typeof data.kind !== "string") return null;
  const view = CALL_VIEWS[data.kind];
  if (view === undefined) return null;
  if (view.accepts !== undefined && !view.accepts(data)) return null;
  return { view, data };
}
