import { describe, expect, it } from "vitest";

import { readBrowserPageDisplay } from "@/components/assistant-ui/tools/browser-page-display";
import { resolveCallView } from "@/components/assistant-ui/tools/call-view-registry";
import { browserPageView } from "@/components/assistant-ui/tools/views/browser-page-view";
import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

const PAGE = { kind: "browser-page", tab_id: "t2", url: "https://example.com/", title: "Example" };
const SHA = "a".repeat(64);

describe("browser-page display", () => {
  it("reads page, screenshot and handoff fields", () => {
    expect(readBrowserPageDisplay(PAGE)).toEqual({ tabId: "t2", url: "https://example.com/", title: "Example" });
    expect(readBrowserPageDisplay({ ...PAGE, screenshot: SHA, handoff_reason: "sign in" })).toMatchObject({
      screenshot: SHA,
      handoffReason: "sign in",
    });
  });

  // 目的：截图地址只来自合法的附件 id；潜在缺陷：展示数据里的任意字符串被拼进图片地址。
  it("drops malformed screenshot ids and rejects malformed pages", () => {
    expect(readBrowserPageDisplay({ ...PAGE, screenshot: "../../etc" })?.screenshot).toBeUndefined();
    expect(readBrowserPageDisplay({ ...PAGE, tab_id: 2 })).toBeNull();
    expect(readBrowserPageDisplay({ ...PAGE, url: 3 })).toBeNull();
  });

  // 目的：卡片列出 act 的每一步，停下的那步一眼可见；潜在缺陷：坏载荷让卡片崩溃，或有失败时仍收着。
  it("reads act steps and opens the card when a step failed", () => {
    const steps = [
      { action: "click", target: 'button "Add"', status: "done" },
      { action: "type", target: "e9", status: "failed", hint: "元素已失效" },
      { action: "press", target: null, status: "skipped" },
    ];
    expect(readBrowserPageDisplay({ ...PAGE, steps })?.steps).toEqual(steps);
    expect(readBrowserPageDisplay({ ...PAGE, steps: [{ action: "click", target: 3, status: "done" }] })?.steps).toBeUndefined();
    const context = { taskId: 1, toolCallId: "c", artifact: {} as ToolArtifact, args: {}, interactive: true, taskCancelling: false };
    expect(browserPageView.defaultOpen!({ ...PAGE, steps }, context)).toBe(true);
    expect(browserPageView.defaultOpen!({ ...PAGE, steps: steps.slice(0, 1) }, context)).toBe(false);
  });

  it("routes browser tools by display kind, falling back on bad payloads", () => {
    const artifact = (display_data: Record<string, unknown>): ToolArtifact => ({
      backendStatus: "completed", awaitingUser: false, background: false, display_data, error: null, errorCode: null,
    });
    expect(resolveCallView(artifact(PAGE))?.view).toBe(browserPageView);
    expect(resolveCallView(artifact({ ...PAGE, handoff_reason: "sign in" }))?.view).toBe(browserPageView);
    expect(resolveCallView(artifact({ kind: "browser-page" }))).toBeNull();
  });
});
