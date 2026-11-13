import type { BrowserDialog } from "../../shared/host-contract";
import type { ActOutcome, EvaluateResult, LogsResult, PageHead, PageResult, ScreenshotResult, TaskTab } from "./browser-host";

/**
 * `browser.*` 方法的出站适配：领域结果（驼峰）→ 后端读取的线上形状（蛇形）。与入站的 `params.ts` 对称，线上形状
 * 只在这两处出现。后端按这些键读取（`app/tools/tool_handler/browser/`）；两边一致由同一份样例守着：宿主单测断言
 * 这里的输出等于 `contract/wire-samples.json`，后端守卫测试用同一份样例渲染正文。
 */

function pageHeadWire(head: PageHead) {
  const { width, height, mobile, colorScheme, follows } = head.emulation;
  return {
    tab_id: head.tabId,
    url: head.url,
    title: head.title,
    loading: head.loading,
    status: head.status,
    emulation: { width, height, mobile, color_scheme: colorScheme, follows_panel: follows },
  };
}

export function pageWire(page: PageResult) {
  return {
    ...pageHeadWire(page),
    snapshot: page.snapshot,
    frames: page.frames,
    notes: page.notes,
    new_logs: {
      console_errors: page.newLogs.consoleErrors,
      console_warnings: page.newLogs.consoleWarnings,
      failed_requests: page.newLogs.failedRequests,
    },
    other_tabs: page.otherTabs,
    dialog: dialogWire(page.dialog),
  };
}

export function actWire(outcome: ActOutcome) {
  return { ...pageHeadWire(outcome), execution: outcome.execution, observation: outcome.observation ? pageWire(outcome.observation) : null, observation_error: outcome.observationError };
}

export function logsWire(logs: LogsResult) {
  const entries = logs.kind === "console"
    ? logs.entries.map(({ seq, level, text, source, frame }) => ({ seq, level, text, source, frame }))
    : logs.entries.map(({ seq, method, url, status, resourceType, failure, durationMs }) => ({
      seq, method, url, status, resource_type: resourceType, failure, duration_ms: durationMs,
    }));
  return {
    ...pageHeadWire(logs),
    kind: logs.kind,
    entries,
    next: logs.next,
    more_before: logs.moreBefore,
    more_after: logs.moreAfter,
    dropped_before: logs.droppedBefore,
  };
}

export function evaluateWire(result: EvaluateResult) {
  return { ...pageHeadWire(result), value: result.value, truncated: result.truncated, error: result.error, dialog: dialogWire(result.dialog) };
}

export function screenshotWire(shot: ScreenshotResult) {
  return { ...pageHeadWire(shot), image: shot.image };
}

export function tabsWire(tabs: TaskTab[]) {
  return tabs.map((tab) => ({ tab_id: tab.tabId, title: tab.title, url: tab.url, current: tab.current, created_by: tab.createdBy, operation: tab.operation }));
}

export function taskIdsWire(taskIds: number[]) {
  return { task_ids: taskIds };
}

function dialogWire(dialog: BrowserDialog | null) {
  return dialog && { id: dialog.id, type: dialog.type, message: dialog.message, default_text: dialog.defaultText };
}
