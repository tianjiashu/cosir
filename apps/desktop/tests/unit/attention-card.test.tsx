import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
// 服务端渲染读 zustand 初始状态：交接登记表换成可控的表。
const handoffs = vi.hoisted(() => new Map<string, { toolCallId: string; toolName: string; resourceId: string; reason: string }>());
vi.mock("@/lib/handoff/handoff-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/handoff/handoff-store")>(),
  useHandoffStore: (selector: (state: { sessions: Record<number, unknown[]> }) => unknown) => selector({ sessions: { 7: [...handoffs.values()] } }),
}));

import { AttentionCard, attentionDescription } from "@/components/conversation/turn/attention-card";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";

function artifact(overrides: Partial<ToolArtifact> = {}): ToolArtifact {
  return {presentation: "activity",
    backendStatus: "pending",
    awaitingUser: true,
    background: false,
    display_data: { kind: "browser-page", tab_id: "t1", url: "https://a.test/login", title: "登录", handoff_reason: "在页面上完成登录后交还" },
    error: null,
    errorCode: null,
    ...overrides,
  };
}

function render(overrides: Partial<ToolArtifact> = {}, interactive = true, toolName = "browser_handoff"): string {
  return renderToStaticMarkup(
    <AttentionCard sessionId={7} toolCallId="call-1" toolName={toolName} args={{ tab: "t1", reason: "完成登录" }} artifact={artifact(overrides)} interactive={interactive} />,
  );
}

beforeEach(() => handoffs.clear());

describe("AttentionCard", () => {
  it("标题按工具目录取动词，说明取交接原因；是对话流里唯一带边框、用信息蓝的容器", () => {
    const html = render();
    expect(html).toContain("交给你操作网页");
    expect(html).toContain("在页面上完成登录后交还");
    expect(html).toContain("border-[color-mix(in_oklab,var(--info)_38%,transparent)]");
    expect(html).toContain("lucide-globe");
  });

  it("有交接登记时给「打开面板」+「交还」；登记已清（后端重启）时仍可交还", () => {
    handoffs.set("call-1", { toolCallId: "call-1", toolName: "browser_handoff", resourceId: "t1", reason: "x" });
    const withEntry = render();
    expect(withEntry).toContain("打开面板");
    expect(withEntry).toContain(">交还<");
    handoffs.clear();
    const without = render();
    expect(without).not.toContain("打开面板");
    expect(without).toContain(">交还<");
  });

  it("只读面不提供交还；非交接类等待只有标题与说明", () => {
    expect(render({}, false)).not.toContain(">交还<");
    const generic = render({ display_data: { kind: "future-decision", status_hint: "请在面板里作答" } }, true, "future_tool");
    expect(generic).toContain("future_tool");
    expect(generic).toContain("请在面板里作答");
    expect(generic).not.toContain(">交还<");
  });

  it("浏览器交接的说明取 handoff_reason", () => {
    expect(attentionDescription(artifact({
      display_data: { kind: "browser-page", tab_id: "t1", url: "https://a.test", title: "A", handoff_reason: "完成登录" },
    }))).toBe("完成登录");
  });
});
