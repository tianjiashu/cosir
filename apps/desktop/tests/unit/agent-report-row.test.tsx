import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("@assistant-ui/react", () => ({ MessagePrimitive: {}, useAuiState: () => null, useScrollLock: () => () => undefined }));
vi.mock("@/components/links/use-open-session", () => ({ useOpenSession: () => () => undefined }));
vi.mock("@/lib/assistant/turn/disclosure-store", () => ({ agentKey: (id: string) => `agent:${id}`, useDisclosure: () => [false, () => undefined] }));

import { AgentMessageRow } from "@/components/conversation/thread/agent-message";
import type { TransportMessageSource } from "@/lib/assistant/contract";

function row(report: TransportMessageSource["report"]): string {
  return renderToStaticMarkup(<AgentMessageRow id="11" text="结论" source={{ sessionId: 4, name: "review", report }} sessionId={1} />);
}

describe("派生 Session 的汇报行", () => {
  // 目的：汇报的结局是编排给出的事实；潜在缺陷：完成也挂标记吵闹，或失败、中间结果不说。
  it("完成不说话，中间结果与失败标在行上；普通 Agent 消息没有标记", () => {
    expect(row(null)).toContain("来自 review");
    expect(row(null)).not.toContain("·");
    expect(row({ status: "completed", intermediate: false })).not.toContain("·");
    expect(row({ status: "completed", intermediate: true })).toContain("· 中间结果");
    expect(row({ status: "failed", intermediate: false })).toContain("· 失败");
  });
});
