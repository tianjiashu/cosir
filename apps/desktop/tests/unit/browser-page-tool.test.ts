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
    expect(readBrowserPageDisplay({ ...PAGE, tab_id: "tab-2" })).toBeNull();
    expect(readBrowserPageDisplay({ ...PAGE, url: 3 })).toBeNull();
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
