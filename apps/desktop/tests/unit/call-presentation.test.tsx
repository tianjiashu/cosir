import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { resolveCallView } from "@/components/conversation/tools/call-view-registry";
import type { CallViewContext } from "@/components/conversation/tools/call-view";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";

const resolve = (display_data: Record<string, unknown>) => {
  const artifact: ToolArtifact = { presentation: "activity", backendStatus: "completed", awaitingUser: false, background: false, display_data, error: null, errorCode: null };
  const context: CallViewContext = { artifact, sessionId: 1, toolCallId: "c", toolName: "probe", args: {}, interactive: true, sessionCancelling: false };
  return resolveCallView(context);
};

it.each(["unknown", "constructor", "toString", "__proto__"])("unknown kind %s never dispatches inherited properties", kind => {
  expect(resolve({ kind, meta: "unexpected", detail: {} })).toBeNull();
});

describe("detail availability without building body", () => {
  it.each([
    ["file-list", {}, "未找到匹配"],
    ["content-search-results", {}, "未找到匹配"],
    ["web-search-results", {}, "未找到匹配"],
    ["web-extract-urls", {}, "未找到匹配"],
    ["mcp-tool-result", { action: "list" }, "没有已启用的 MCP 服务"],
    ["skill-content", { action: "list" }, "没有可用 Skill"],
  ])("%s keeps an expandable empty-state detail", (kind, fields, label) => {
    const detail = resolve({ kind, ...fields })?.detail;
    expect(detail).toBeDefined();
    expect(renderToStaticMarkup(<>{detail!.render()}</>)).toContain(label);
  });

  it.each([
    ["read-file-meta", {}], ["file-changes", { changes: [] }],
    ["mcp-tool-result", { action: "call" }], ["skill-content", { action: "load" }],
    ["terminal-session", {}],
  ])("%s keeps summary-only payloads non-expandable", (kind, fields) => {
    expect(resolve({ kind, ...fields })?.detail).toBeUndefined();
  });

  it("does not enumerate a large list until the body is requested", () => {
    let reads = 0;
    const files = Array.from({ length: 1000 }, (_, i) => ({ get path() { reads++; return `file-${i}.ts`; } }));
    const presentation = resolve({ kind: "file-list", files });
    expect(presentation?.meta).toContain("1000 个文件");
    expect(reads).toBe(0);
    presentation!.detail!.render();
    expect(reads).toBeGreaterThanOrEqual(1000);
  });
});
