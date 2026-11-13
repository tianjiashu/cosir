import { detailsLiveInRail, type CallView, type CallPresentation, type CallViewContext } from "./call-view";
import { mcpView, skillView } from "./call-views/extension-views";
import { imageReadView } from "./call-views/image-read-view";
import { readFileView } from "./call-views/read-file-view";
import { webExtractView, webSearchView } from "./call-views/web-views";
import { contentSearchView, directoryListView, fileListView } from "./call-views/workspace-list-views";
import { asRecord } from "./display-data-readers";
import { browserPageView } from "./call-views/browser-page-view";
import { agentTaskResultView, delegationView, forkTaskView } from "./call-views/child-task-views";
import { fileChangesView } from "./call-views/file-changes-view";
import { terminalResultView } from "./call-views/terminal-result-view";
import { terminalSessionView } from "./call-views/terminal-session-view";

/**
 * `display_data.kind` → 调用行视图。这是前端唯一的工具展示路由：只按 kind，不按工具名、不按静态展示声明。
 * 新增一种工具展示只需在这里登记一个 kind。
 * 配置草稿仍在产物区成卡。
 */
export const CALL_VIEWS: Readonly<Record<string, CallView>> = {
  "read-file-meta": readFileView,
  "file-list": fileListView,
  "content-search-results": contentSearchView,
  "directory-list": directoryListView,
  "web-search-results": webSearchView,
  "web-extract-urls": webExtractView,
  "image-read": imageReadView,
  "mcp-tool-result": mcpView,
  "skill-content": skillView,
  "terminal-result": terminalResultView,
  "terminal-session": terminalSessionView,
  "file-changes": fileChangesView,
  "delegation-result": delegationView,
  "fork-task-result": forkTaskView,
  "agent-task-result": agentTaskResultView,
  "browser-page": browserPageView,
};

/** 按 kind 解析一次；未知 kind 或无效载荷退化纯行，不猜工具名。归侧栏的调用在这里收成一行，新视图无需自己判断。 */
export function resolveCallView(context: CallViewContext): CallPresentation | null {
  const data = asRecord(context.artifact.display_data);
  if (typeof data.kind !== "string" || !Object.hasOwn(CALL_VIEWS, data.kind)) return null;
  const view = CALL_VIEWS[data.kind]?.(data, context) ?? null;
  return view && detailsLiveInRail(context) ? { ...view, detail: undefined, actions: undefined, overlay: undefined } : view;
}
