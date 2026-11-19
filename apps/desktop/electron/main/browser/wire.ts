import type { BrowserDialog } from "../../shared/host-contract";
import type { ActOutcome, EvaluateResult, LogsResult, PageHead, PageResult, ScreenshotResult, SessionTab } from "./browser-host";
import type { ConsoleEntry, NetworkEntry } from "./tab-log";

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

function networkEntryWire({ seq, method, url, status, resourceType, failure, durationMs, body }: NetworkEntry) {
  return { seq, method, url, status, resource_type: resourceType, failure, duration_ms: durationMs, body: body && { state: body.state, mime: body.mime, size: body.size } };
}

export function logsWire(logs: LogsResult) {
  if (logs.kind === "response_body") {
    const { state, mime, size, text, truncated, reason } = logs.body;
    return { ...pageHeadWire(logs), kind: logs.kind, entry: networkEntryWire(logs.entry), body: { state, mime, size, text, truncated, reason } };
  }
  const entries = logs.kind === "console"
    ? logs.entries.map(consoleEntryWire)
    : logs.entries.map(networkEntryWire);
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

function consoleEntryWire({ seq, level, text, source, frame }: ConsoleEntry) {
  return { seq, level, text, source, frame };
}

export function evaluateWire(result: EvaluateResult) {
  const { entries, moreAfter, next } = result.console;
  return {
    ...pageHeadWire(result), value: result.value, truncated: result.truncated, error: result.error, dialog: dialogWire(result.dialog),
    console: { entries: entries.map(consoleEntryWire), more_after: moreAfter, next },
  };
}

export function screenshotWire(shot: ScreenshotResult) {
  const cut = shot.fullPageCut;
  return { ...pageHeadWire(shot), image: shot.image, full_page_cut: cut && { page_height: cut.pageHeight, shown_height: cut.shownHeight } };
}

export function tabsWire(tabs: SessionTab[]) {
  return tabs.map((tab) => ({ tab_id: tab.tabId, title: tab.title, url: tab.url, current: tab.current, created_by: tab.createdBy, operation: tab.operation }));
}

export function sessionIdsWire(sessionIds: number[]) {
  return { session_ids: sessionIds };
}

function dialogWire(dialog: BrowserDialog | null) {
  return dialog && { id: dialog.id, type: dialog.type, message: dialog.message, default_text: dialog.defaultText };
}
