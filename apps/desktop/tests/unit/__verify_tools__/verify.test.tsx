import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
vi.mock("@/lib/http/client", async (orig) => ({ ...(await orig<object>()), requestOk: vi.fn().mockResolvedValue({}) }));

import { useCommandRequests, reconcileCommands, commandPending, stopTarget } from "@/lib/assistant/command-request-store";
import { cancelAgentTeamRun } from "@/lib/assistant/task-commands";
import { AgentTeamExecutionCard } from "@/components/conversation/tools/agent-team/agent-team-execution-card";
import { queryKeys } from "@/lib/query/query-keys";
import { toolResultSignal } from "@/lib/assistant/tools/tool-result-signals";
import { parseTerminalOutput } from "@/components/conversation/tools/terminal-output/text";

describe("W1", () => {
  it("TeamRun 停止受理后 stop 槽位不结算，终态卡片仍显示转圈", async () => {
    expect(await cancelAgentTeamRun(1, "call-1", 17)).toBeNull();
    // 订阅随后发布终态 activity，模拟 Task 也落定
    reconcileCommands(1, { status: "completed" } as never);
    const slot = useCommandRequests.getState().tasks[1]?.[stopTarget("call-1")];
    expect(slot?.phase).toBe("syncing");
    expect(commandPending(slot)).toBe(true);
    const client = new QueryClient();
    client.setQueryData(queryKeys.agentTeamRun(17), { id: 17, team_id: "q", status: "cancelled", goal: "g", current_node_id: null, end_reason: "cancelled", nodes: [] });
    const html = renderToStaticMarkup(<QueryClientProvider client={client}><AgentTeamExecutionCard taskId={1} toolCallId="call-1" interactive
      artifact={{} as never} displayOverride={{ teamRunId: 17, teamId: "q", goal: "g" }} /></QueryClientProvider>);
    expect(html).toContain("已停止");
    expect(html).toContain('aria-label="正在停止Agent Team"');
    expect(html).toContain("animate-spin");
  });
});

describe("W3", () => {
  it("最后一步 unknown 时行上无信号", () => {
    expect(toolResultSignal({ kind: "browser-page", steps: [{ action: "click", status: "unknown" }] } as never)).toBeNull();
    expect(toolResultSignal({ kind: "browser-page", steps: [{ status: "unknown" }, { status: "skipped" }] } as never)).not.toBeNull();
  });
});

describe("W2", () => {
  it("大输出解析耗时", async () => {
    for (const size of [32 * 1024, 256 * 1024, 320 * 1024]) {
      const line = "\x1b[32mok\x1b[0m building module foo/bar/baz.ts ... done in 12ms\n";
      const output = line.repeat(Math.ceil(size / line.length)).slice(0, size);
      await parseTerminalOutput(output);
      const t = performance.now();
      const runs = 3;
      for (let i = 0; i < runs; i++) await parseTerminalOutput(output);
      console.log(`W2 size=${size} avg=${((performance.now() - t) / runs).toFixed(1)}ms`);
    }
  }, 60_000);
});
